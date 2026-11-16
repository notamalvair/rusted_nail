use anyhow::{Context, Result};
use nail_engine::Engine;
use nail_exec::ExecConfig;
use nail_risk::{RiskConfig, RiskManager};
use nail_sim::SimConfig;
use nail_strategy::{
    CopyTrade, CopyTradeConfig, ExitRules, Momentum, MomentumConfig, Sniper, SniperConfig, Strategy,
};
use serde::Deserialize;

// Все секции необязательны; пропущенные поля берут значения по умолчанию из библиотек.

#[derive(Deserialize, Default)]
#[serde(default, deny_unknown_fields)]
pub struct Config {
    feed: Feed,
    exec: Exec,
    sim: Sim,
    risk: Risk,
    exits: Exits,
    sniper: SniperSection,
    momentum: MomentumSection,
    copytrade: CopySection,
}

#[derive(Deserialize, Default)]
#[serde(default, deny_unknown_fields)]
struct Feed {
    url: Option<String>,
}

#[derive(Deserialize, Default)]
#[serde(default, deny_unknown_fields)]
struct Exec {
    url: Option<String>,
    slippage: Option<f64>,
    priority_fee: Option<f64>,
}

#[derive(Deserialize, Default)]
#[serde(default, deny_unknown_fields)]
struct Sim {
    fee_rate: Option<f64>,
    slippage: Option<f64>,
    latency_ms: Option<u64>,
}

#[derive(Deserialize, Default)]
#[serde(default, deny_unknown_fields)]
struct Risk {
    max_position_sol: Option<f64>,
    max_open_positions: Option<usize>,
    max_exposure_sol: Option<f64>,
    daily_loss_limit_sol: Option<f64>,
}

#[derive(Deserialize, Default)]
#[serde(default, deny_unknown_fields)]
struct Exits {
    take_profit_pct: Option<f64>,
    stop_loss_pct: Option<f64>,
    trailing_stop_pct: Option<f64>,
    max_hold_ms: Option<u64>,
    exit_on_dev_sell: Option<bool>,
}

#[derive(Deserialize, Default)]
#[serde(default, deny_unknown_fields)]
struct SniperSection {
    enabled: bool,
    size_sol: Option<f64>,
    max_launches_per_creator: Option<u32>,
    creator_window_ms: Option<u64>,
    blacklist: Vec<String>,
}

#[derive(Deserialize, Default)]
#[serde(default, deny_unknown_fields)]
struct MomentumSection {
    enabled: bool,
    size_sol: Option<f64>,
    window_ms: Option<u64>,
    min_net_buy_sol: Option<f64>,
    min_unique_buyers: Option<usize>,
    min_mcap_sol: Option<f64>,
    max_mcap_sol: Option<f64>,
    cooldown_ms: Option<u64>,
}

#[derive(Deserialize, Default)]
#[serde(default, deny_unknown_fields)]
struct CopySection {
    enabled: bool,
    size_sol: Option<f64>,
    wallets: Vec<String>,
}

pub fn load(path: &str) -> Result<Config> {
    let text = std::fs::read_to_string(path).with_context(|| format!("read {path}"))?;
    toml::from_str(&text).with_context(|| format!("parse {path}"))
}

impl Config {
    pub fn feed_url(&self) -> String {
        self.feed
            .url
            .clone()
            .unwrap_or_else(|| nail_feed::DEFAULT_URL.into())
    }

    pub fn exec(&self) -> ExecConfig {
        let d = ExecConfig::default();
        ExecConfig {
            url: self.exec.url.clone().unwrap_or(d.url),
            slippage: self.exec.slippage.unwrap_or(d.slippage),
            priority_fee: self.exec.priority_fee.unwrap_or(d.priority_fee),
        }
    }

    pub fn sim(&self) -> SimConfig {
        let d = SimConfig::default();
        SimConfig {
            fee_rate: self.sim.fee_rate.unwrap_or(d.fee_rate),
            slippage: self.sim.slippage.unwrap_or(d.slippage),
            latency_ms: self.sim.latency_ms.unwrap_or(d.latency_ms),
        }
    }

    pub fn engine(&self) -> Engine {
        let mut strategies: Vec<Box<dyn Strategy>> = Vec::new();
        if self.sniper.enabled {
            let (s, d) = (&self.sniper, SniperConfig::default());
            strategies.push(Box::new(Sniper::new(SniperConfig {
                size_sol: s.size_sol.unwrap_or(d.size_sol),
                max_launches_per_creator: s
                    .max_launches_per_creator
                    .unwrap_or(d.max_launches_per_creator),
                creator_window_ms: s.creator_window_ms.unwrap_or(d.creator_window_ms),
                blacklist: s.blacklist.iter().cloned().collect(),
            })));
        }
        if self.momentum.enabled {
            let (m, d) = (&self.momentum, MomentumConfig::default());
            strategies.push(Box::new(Momentum::new(MomentumConfig {
                size_sol: m.size_sol.unwrap_or(d.size_sol),
                window_ms: m.window_ms.unwrap_or(d.window_ms),
                min_net_buy_sol: m.min_net_buy_sol.unwrap_or(d.min_net_buy_sol),
                min_unique_buyers: m.min_unique_buyers.unwrap_or(d.min_unique_buyers),
                min_mcap_sol: m.min_mcap_sol.unwrap_or(d.min_mcap_sol),
                max_mcap_sol: m.max_mcap_sol.unwrap_or(d.max_mcap_sol),
                cooldown_ms: m.cooldown_ms.unwrap_or(d.cooldown_ms),
            })));
        }
        if self.copytrade.enabled {
            let (c, d) = (&self.copytrade, CopyTradeConfig::default());
            strategies.push(Box::new(CopyTrade::new(CopyTradeConfig {
                size_sol: c.size_sol.unwrap_or(d.size_sol),
                wallets: c.wallets.iter().cloned().collect(),
            })));
        }

        let (e, d) = (&self.exits, ExitRules::default());
        let exits = ExitRules {
            take_profit_pct: e.take_profit_pct.unwrap_or(d.take_profit_pct),
            stop_loss_pct: e.stop_loss_pct.unwrap_or(d.stop_loss_pct),
            trailing_stop_pct: e.trailing_stop_pct,
            max_hold_ms: e.max_hold_ms.unwrap_or(d.max_hold_ms),
            exit_on_dev_sell: e.exit_on_dev_sell.unwrap_or(d.exit_on_dev_sell),
        };
        let (r, d) = (&self.risk, RiskConfig::default());
        let risk = RiskConfig {
            max_position_sol: r.max_position_sol.unwrap_or(d.max_position_sol),
            max_open_positions: r.max_open_positions.unwrap_or(d.max_open_positions),
            max_exposure_sol: r.max_exposure_sol.unwrap_or(d.max_exposure_sol),
            daily_loss_limit_sol: r.daily_loss_limit_sol.unwrap_or(d.daily_loss_limit_sol),
        };
        Engine::new(strategies, exits, RiskManager::new(risk))
    }
}
