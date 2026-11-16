//! rusted_nail [config.toml] [--live]
//! По умолчанию paper-режим: живой стрим, виртуальные сделки. Реальные деньги только с --live.

use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result};
use nail_bot::{config, paper_step};
use nail_core::{Event, Fill, Intent};
use nail_exec::PumpApiClient;
use nail_sim::PaperExecutor;
use tokio::sync::mpsc;

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

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_millis() as u64)
}

fn log_fill(reason: &str, fill: &Fill, pnl: Option<f64>) {
    println!(
        "[fill] {:?} {} tokens={:.0} sol={:.5} pnl={} | {}",
        fill.side,
        fill.mint,
        fill.token_amount,
        fill.sol_amount,
        pnl.map_or("-".into(), |p| format!("{p:+.5}")),
        reason
    );
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

    let mut stats = tokio::time::interval(Duration::from_secs(30));
    let mut events: u64 = 0;
    // Отставание стрима: наши часы минус время блока. Грубая оценка, зависит от синхронизации часов.
    let (mut lag_sum, mut lag_max, mut lag_n) = (0u64, 0u64, 0u64);

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

    loop {
        tokio::select! {
            ev = ev_rx.recv() => {
                let Some(ev) = ev else { break };
                events += 1;
                let lag = now_ms().saturating_sub(ev.ts_ms());
                lag_sum += lag;
                lag_max = lag_max.max(lag);
                lag_n += 1;
                match &live {
                    None => paper_step(&mut engine, &mut sim, &ev, |i, f, pnl| {
                        log_fill(&i.reason, f, pnl);
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
                                log_fill("confirmed on stream", &fill, pnl);
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
