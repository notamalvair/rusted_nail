use std::collections::{HashMap, HashSet, VecDeque};

use nail_core::{Amount, Event, Intent, Mint, Pool, Side, Wallet};

use crate::Strategy;

#[derive(Clone, Debug)]
pub struct MomentumConfig {
    pub size_sol: f64,
    pub window_ms: u64,
    /// Чистый приток (покупки минус продажи) за окно, SOL.
    pub min_net_buy_sol: f64,
    pub min_unique_buyers: usize,
    pub min_mcap_sol: f64,
    pub max_mcap_sol: f64,
    /// Пауза между сигналами по одному токену.
    pub cooldown_ms: u64,
}

impl Default for MomentumConfig {
    fn default() -> Self {
        Self {
            size_sol: 0.03,
            window_ms: 5_000,
            min_net_buy_sol: 3.0,
            min_unique_buyers: 5,
            min_mcap_sol: 35.0,
            max_mcap_sol: 200.0,
            cooldown_ms: 60_000,
        }
    }
}

#[derive(Default)]
struct Window {
    trades: VecDeque<(u64, f64, Wallet)>,
    net_sol: f64,
    last_signal_ms: Option<u64>,
}

/// Вход по всплеску чистого притока на bonding curve.
pub struct Momentum {
    cfg: MomentumConfig,
    windows: HashMap<Mint, Window>,
    seen: u64,
}

impl Momentum {
    pub const NAME: &'static str = "momentum";

    pub fn new(cfg: MomentumConfig) -> Self {
        Self {
            cfg,
            windows: HashMap::new(),
            seen: 0,
        }
    }
}

impl Strategy for Momentum {
    fn name(&self) -> &'static str {
        Self::NAME
    }

    fn on_event(&mut self, ev: &Event) -> Option<Intent> {
        let Event::Trade(t) = ev else { return None };
        if t.pool != Pool::Pump {
            return None;
        }
        let now = t.ts_ms;
        let cutoff = now.saturating_sub(self.cfg.window_ms);

        self.seen += 1;
        if self.seen.is_multiple_of(50_000) {
            let stale = now.saturating_sub(self.cfg.window_ms.max(self.cfg.cooldown_ms));
            self.windows
                .retain(|_, w| w.trades.back().is_some_and(|x| x.0 >= stale));
        }

        let w = self.windows.entry(t.mint.clone()).or_default();
        let signed = match t.side {
            Side::Buy => t.sol_amount,
            Side::Sell => -t.sol_amount,
        };
        w.trades.push_back((now, signed, t.trader.clone()));
        w.net_sol += signed;
        while let Some((_, s, _)) = w.trades.pop_front_if(|x| x.0 < cutoff) {
            w.net_sol -= s;
        }

        if t.side != Side::Buy
            || w.net_sol < self.cfg.min_net_buy_sol
            || t.market_cap_sol < self.cfg.min_mcap_sol
            || t.market_cap_sol > self.cfg.max_mcap_sol
            || w.last_signal_ms
                .is_some_and(|l| now.saturating_sub(l) < self.cfg.cooldown_ms)
        {
            return None;
        }
        let buyers: HashSet<&str> = w
            .trades
            .iter()
            .filter(|x| x.1 > 0.0)
            .map(|x| x.2.as_str())
            .collect();
        if buyers.len() < self.cfg.min_unique_buyers {
            return None;
        }
        w.last_signal_ms = Some(now);
        Some(Intent {
            strategy: Self::NAME,
            mint: t.mint.clone(),
            side: Side::Buy,
            amount: Amount::Sol(self.cfg.size_sol),
            reason: format!(
                "net +{:.2} SOL / {} buyers in {} ms, mcap {:.0} SOL",
                w.net_sol,
                buyers.len(),
                self.cfg.window_ms,
                t.market_cap_sol
            ),
        })
    }
}
