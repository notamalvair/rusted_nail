//! rusted_nail [config.toml] [--live] [--for-secs=N]
//! По умолчанию paper-режим: живой стрим, виртуальные сделки. Реальные деньги только с --live.
//! --for-secs=N: остановиться через N секунд (для контролируемых прогонов).

use std::collections::VecDeque;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result};
use nail_bot::ui::{self, Command, FillRow, PositionRow, Snapshot, StrategyRow};
use nail_bot::{config, paper_step};
use nail_core::{Event, Fill, Intent, Side};
use nail_engine::Engine;
use nail_exec::PumpApiClient;
use nail_sim::PaperExecutor;
use tokio::sync::{mpsc, watch};

struct Live {
    client: Arc<PumpApiClient>,
    wallet: String,
}

enum ExecMsg {
    Sent {
        mint: String,
        signature: String,
        latency: Duration,
    },
    Failed {
        mint: String,
        err: String,
    },
}

const RECENT_FILLS: usize = 50;

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_millis() as u64)
}

fn log_fill(recent: &mut VecDeque<FillRow>, reason: &str, fill: &Fill, pnl: Option<f64>) {
    println!(
        "[fill] {:?} {} tokens={:.0} sol={:.5} pnl={} | {}",
        fill.side,
        fill.mint,
        fill.token_amount,
        fill.sol_amount,
        pnl.map_or("-".into(), |p| format!("{p:+.5}")),
        reason
    );
    if recent.len() == RECENT_FILLS {
        recent.pop_back();
    }
    recent.push_front(FillRow {
        ts_ms: fill.ts_ms,
        buy: fill.side == Side::Buy,
        mint: fill.mint.clone(),
        sol: fill.sol_amount,
        pnl_sol: pnl,
        reason: reason.to_owned(),
    });
}

fn snapshot(
    engine: &Engine,
    live: bool,
    events: u64,
    lag_ms: u64,
    recent: &VecDeque<FillRow>,
) -> Snapshot {
    let now = now_ms();
    let mut positions: Vec<PositionRow> = engine
        .portfolio
        .positions
        .iter()
        .map(|(mint, p)| PositionRow {
            mint: mint.clone(),
            strategy: p.strategy,
            cost_sol: p.cost_sol,
            pnl_pct: p.pnl_pct(),
            age_s: now.saturating_sub(p.opened_ms) / 1000,
            closing: engine.is_pending(mint),
        })
        .collect();
    positions.sort_by_key(|p| p.age_s);
    Snapshot {
        live,
        events,
        lag_ms,
        kill_switch: engine.risk.kill_switch(),
        halted: engine.risk.halted(),
        day_pnl_sol: engine.risk.day_pnl_sol(),
        realized_sol: engine.portfolio.realized_pnl_sol,
        exposure_sol: engine.portfolio.exposure_sol(),
        risk_rejects: engine.rejected,
        risky_skipped: engine.risky_skipped,
        strategies: engine
            .strategies()
            .into_iter()
            .map(|(name, enabled)| {
                let s = engine.stats.get(name).cloned().unwrap_or_default();
                StrategyRow {
                    name,
                    enabled,
                    buys: s.buys,
                    sells: s.sells,
                    wins: s.wins,
                    pnl_sol: s.pnl_sol,
                }
            })
            .collect(),
        positions,
        fills: recent.iter().cloned().collect(),
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    let _ = rustls::crypto::ring::default_provider().install_default();

    let args: Vec<String> = std::env::args().skip(1).collect();
    let live_flag = args.iter().any(|a| a == "--live");
    let path = args
        .iter()
        .find(|a| !a.starts_with("--"))
        .map_or("config.toml", String::as_str);
    let run_for = match args.iter().find_map(|a| a.strip_prefix("--for-secs=")) {
        Some(v) => Some(Duration::from_secs(
            v.parse().context("--for-secs expects whole seconds")?,
        )),
        None => None,
    };
    let cfg = config::load(path)?;

    let mut engine = cfg.engine();
    let mut sim = PaperExecutor::new(cfg.sim());
    let live = if live_flag {
        let key = std::env::var("PUMPAPI_API_KEY").context("PUMPAPI_API_KEY is not set")?;
        let wallet =
            std::env::var("PUMPAPI_PUBLIC_KEY").context("PUMPAPI_PUBLIC_KEY is not set")?;
        Some(Live {
            client: Arc::new(PumpApiClient::new(key, cfg.exec())?),
            wallet,
        })
    } else {
        None
    };
    println!(
        "[bot] mode: {}",
        if live.is_some() {
            "LIVE (real funds)"
        } else {
            "paper"
        }
    );

    let (ev_tx, mut ev_rx) = mpsc::channel::<Event>(65_536);
    tokio::spawn(nail_feed::run(cfg.feed_url(), ev_tx));
    let (ex_tx, mut ex_rx) = mpsc::unbounded_channel::<ExecMsg>();

    // Панель управления: команды приходят в этот цикл, состояние уходит снимком раз в секунду.
    let (cmd_tx, mut cmd_rx) = mpsc::unbounded_channel::<Command>();
    let (state_tx, state_rx) = watch::channel(String::from("{}"));
    if let Some(addr) = cfg.ui_addr()? {
        let cmd_tx = cmd_tx.clone();
        tokio::spawn(async move {
            if let Err(e) = ui::serve(addr, state_rx, cmd_tx).await {
                eprintln!("[ui] stopped: {e:#}");
            }
        });
    }

    if let Some(l) = &live {
        let client = l.client.clone();
        tokio::spawn(async move {
            let mut tick = tokio::time::interval(Duration::from_secs(15));
            loop {
                tick.tick().await;
                client.warm().await;
            }
        });
    }

    let mut stats = tokio::time::interval(Duration::from_secs(30));
    let mut ui_tick = tokio::time::interval(Duration::from_secs(1));
    let mut events: u64 = 0;
    // Отставание стрима: наши часы минус время блока. Грубая оценка, зависит от синхронизации часов.
    let (mut lag_sum, mut lag_max, mut lag_n, mut lag_last) = (0u64, 0u64, 0u64, 0u64);
    let mut recent: VecDeque<FillRow> = VecDeque::with_capacity(RECENT_FILLS);
    // Без --for-secs таймер стоит на 10 лет и не срабатывает.
    let deadline = tokio::time::sleep(run_for.unwrap_or(Duration::from_secs(10 * 365 * 86_400)));
    tokio::pin!(deadline);

    loop {
        tokio::select! {
            () = &mut deadline => {
                println!("[bot] --for-secs elapsed, stopping");
                break;
            }
            ev = ev_rx.recv() => {
                let Some(ev) = ev else { break };
                events += 1;
                lag_last = now_ms().saturating_sub(ev.ts_ms());
                lag_sum += lag_last;
                lag_max = lag_max.max(lag_last);
                lag_n += 1;

                match &live {
                    None => paper_step(&mut engine, &mut sim, &ev, |i, f, pnl| {
                        log_fill(&mut recent, &i.reason, f, pnl);
                    }),
                    Some(l) => {
                        // Исполнение подтверждается нашей же сделкой из стрима.
                        // TODO: брать точные суммы с комиссиями из preBalances/postBalances.
                        if let Event::Trade(t) = &ev {
                            if t.trader == l.wallet && engine.is_pending(&t.mint) {
                                let fill = Fill {
                                    mint: t.mint.clone(),
                                    side: t.side,
                                    token_amount: t.token_amount,
                                    sol_amount: t.sol_amount,
                                    ts_ms: t.ts_ms,
                                };
                                let pnl = engine.on_fill(&fill);
                                log_fill(&mut recent, "confirmed on stream", &fill, pnl);
                            }
                        }
                        for intent in engine.on_event(&ev) {
                            submit(l.client.clone(), intent, ex_tx.clone());
                        }
                    }
                }
            }
            Some(msg) = ex_rx.recv() => match msg {
                ExecMsg::Sent { mint, signature, latency } => {
                    println!("[exec] sent {mint} in {} ms: {signature}", latency.as_millis());
                }
                ExecMsg::Failed { mint, err } => {
                    eprintln!("[exec] FAILED {mint}: {err}");
                    engine.on_reject(&mint);
                }
            },
            Some(cmd) = cmd_rx.recv() => {
                let now = now_ms();
                let intents = match cmd {
                    Command::Kill(on) => {
                        println!("[ui] kill switch: {on}");
                        engine.risk.set_kill_switch(on);
                        Vec::new()
                    }
                    Command::Strategy { name, enabled } => {
                        if engine.set_strategy_enabled(&name, enabled) {
                            println!("[ui] strategy {name}: buys enabled = {enabled}");
                        }
                        Vec::new()
                    }
                    Command::Close(mint) => engine.close(&mint, now).into_iter().collect(),
                    Command::CloseAll => engine.close_all(now),
                };
                for intent in intents {
                    println!("[ui] {} {}", intent.reason, intent.mint);
                    match &live {
                        // Исполнится на ближайшем событии стрима, с обычной задержкой.
                        None => sim.submit(intent, now),
                        Some(l) => submit(l.client.clone(), intent, ex_tx.clone()),
                    }
                }
            }
            _ = ui_tick.tick() => {
                let snap = snapshot(&engine, live.is_some(), events, lag_last, &recent);
                if let Ok(json) = serde_json::to_string(&snap) {
                    state_tx.send_replace(json);
                }
            }
            _ = stats.tick() => {
                println!(
                    "[stats] events={events} lag_avg={}ms lag_max={lag_max}ms open={} exposure={:.4} realized={:+.5} day={:+.5} risk_rejects={} halted={}",
                    lag_sum.checked_div(lag_n).unwrap_or(0),
                    engine.portfolio.positions.len(),
                    engine.portfolio.exposure_sol(),
                    engine.portfolio.realized_pnl_sol,
                    engine.risk.day_pnl_sol(),
                    engine.rejected,
                    engine.risk.halted(),
                );
                (lag_sum, lag_max, lag_n) = (0, 0, 0);
            }
        }
    }
    println!(
        "[bot] final: events={events} open={} realized={:+.5} day={:+.5}",
        engine.portfolio.positions.len(),
        engine.portfolio.realized_pnl_sol,
        engine.risk.day_pnl_sol(),
    );
    Ok(())
}

fn submit(client: Arc<PumpApiClient>, intent: Intent, tx: mpsc::UnboundedSender<ExecMsg>) {
    println!(
        "[exec] {:?} {} | {} | {}",
        intent.side, intent.mint, intent.strategy, intent.reason
    );
    tokio::spawn(async move {
        let msg = match client.trade(&intent).await {
            Ok(s) => ExecMsg::Sent {
                mint: intent.mint,
                signature: s.signature,
                latency: s.latency,
            },
            Err(e) => ExecMsg::Failed {
                mint: intent.mint,
                err: format!("{e:#}"),
            },
        };
        let _ = tx.send(msg);
    });
}
