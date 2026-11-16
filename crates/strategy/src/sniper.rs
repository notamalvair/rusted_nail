use std::collections::{HashMap, HashSet, VecDeque};

use nail_core::{Amount, Event, Intent, Pool, Side, Wallet};

use crate::Strategy;

#[derive(Clone, Debug)]
pub struct SniperConfig {
    pub size_sol: f64,
    /// Пропускаем создателей, у которых больше стольких запусков за окно (серийные деплоеры).
    pub max_launches_per_creator: u32,
    pub creator_window_ms: u64,
    pub blacklist: HashSet<Wallet>,
}

impl Default for SniperConfig {
    fn default() -> Self {
        Self {
            size_sol: 0.02,
            max_launches_per_creator: 1,
            creator_window_ms: 86_400_000,
            blacklist: HashSet::new(),
        }
    }
}

/// Покупка новых запусков на bonding curve.
pub struct Sniper {
    cfg: SniperConfig,
    launches: HashMap<Wallet, VecDeque<u64>>,
    seen: u64,
}

impl Sniper {
    pub const NAME: &'static str = "sniper";

    pub fn new(cfg: SniperConfig) -> Self {
        Self {
            cfg,
            launches: HashMap::new(),
            seen: 0,
        }
    }
}

impl Strategy for Sniper {
    fn name(&self) -> &'static str {
        Self::NAME
    }

    fn on_event(&mut self, ev: &Event) -> Option<Intent> {
        let Event::Create(c) = ev else { return None };
        if c.pool != Pool::Pump {
            return None;
        }
        let cutoff = c.ts_ms.saturating_sub(self.cfg.creator_window_ms);

        self.seen += 1;
        if self.seen.is_multiple_of(10_000) {
            self.launches
                .retain(|_, q| q.back().is_some_and(|&t| t >= cutoff));
        }

        let q = self.launches.entry(c.creator.clone()).or_default();
        while q.front().is_some_and(|&t| t < cutoff) {
            q.pop_front();
        }
        q.push_back(c.ts_ms);

        if q.len() as u32 > self.cfg.max_launches_per_creator
            || self.cfg.blacklist.contains(&c.creator)
        {
            return None;
        }
        Some(Intent {
            strategy: Self::NAME,
            mint: c.mint.clone(),
            side: Side::Buy,
            amount: Amount::Sol(self.cfg.size_sol),
            reason: format!("new launch {} by {}", c.symbol, c.creator),
        })
    }
}
