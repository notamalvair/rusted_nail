use std::collections::HashSet;

use nail_core::{Amount, Event, Intent, Pool, Side, Wallet};

use crate::Strategy;

#[derive(Clone, Debug)]
pub struct CopyTradeConfig {
    pub size_sol: f64,
    pub wallets: HashSet<Wallet>,
}

impl Default for CopyTradeConfig {
    fn default() -> Self {
        Self { size_sol: 0.03, wallets: HashSet::new() }
    }
}

/// Повторяет покупки и продажи кошельков из списка.
pub struct CopyTrade {
    cfg: CopyTradeConfig,
}

impl CopyTrade {
    pub const NAME: &'static str = "copytrade";

    pub fn new(cfg: CopyTradeConfig) -> Self {
        Self { cfg }
    }
}

impl Strategy for CopyTrade {
    fn name(&self) -> &'static str {
        Self::NAME
    }

    fn on_event(&mut self, ev: &Event) -> Option<Intent> {
        let Event::Trade(t) = ev else { return None };
        if t.pool == Pool::Other || self.cfg.wallets.is_empty() {
            return None;
        }
        let leader = if self.cfg.wallets.contains(&t.trader) {
            &t.trader
        } else {
            t.traders.iter().find(|w| self.cfg.wallets.contains(*w))?
        };
        let amount = match t.side {
            Side::Buy => Amount::Sol(self.cfg.size_sol),
            // Движок продаст только если позиция открыта этой стратегией.
            Side::Sell => Amount::Fraction(1.0),
        };
        Some(Intent {
            strategy: Self::NAME,
            mint: t.mint.clone(),
            side: t.side,
            amount,
            reason: format!("follow {leader} {:?} {:.3} SOL", t.side, t.sol_amount),
        })
    }
}
