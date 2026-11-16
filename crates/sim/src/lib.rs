//! Paper-исполнение: мгновенный fill по последней цене с комиссией и проскальзыванием.
//! TODO(backtest): задержка в слотах и влияние на цену по резервам кривой.

use std::collections::HashMap;

use nail_core::{Amount, Event, Fill, Intent, Mint, Portfolio, Side, PUMP_INITIAL_PRICE_SOL};

#[derive(Clone, Debug)]
pub struct SimConfig {
    /// Суммарная комиссия с оборота на одну сделку (pumpapi + pump.fun).
    pub fee_rate: f64,
    /// Проскальзывание в худшую сторону, доля.
    pub slippage: f64,
}

impl Default for SimConfig {
    fn default() -> Self {
        Self {
            fee_rate: 0.015,
            slippage: 0.03,
        }
    }
}

pub struct PaperExecutor {
    cfg: SimConfig,
    last_price: HashMap<Mint, f64>,
}

impl PaperExecutor {
    pub fn new(cfg: SimConfig) -> Self {
        Self {
            cfg,
            last_price: HashMap::new(),
        }
    }

    /// Вызывать на каждое событие до `fill`.
    pub fn observe(&mut self, ev: &Event) {
        match ev {
            Event::Trade(t) if t.price > 0.0 => {
                self.last_price.insert(t.mint.clone(), t.price);
            }
            Event::Create(c) => {
                self.last_price
                    .entry(c.mint.clone())
                    .or_insert(PUMP_INITIAL_PRICE_SOL);
            }
            _ => {}
        }
    }

    pub fn fill(&self, intent: &Intent, portfolio: &Portfolio, ts_ms: u64) -> Option<Fill> {
        let px = *self.last_price.get(&intent.mint)?;
        match (intent.side, intent.amount) {
            (Side::Buy, Amount::Sol(sol)) => {
                let tokens = sol * (1.0 - self.cfg.fee_rate) / (px * (1.0 + self.cfg.slippage));
                Some(Fill {
                    mint: intent.mint.clone(),
                    side: Side::Buy,
                    token_amount: tokens,
                    sol_amount: sol,
                    ts_ms,
                })
            }
            (Side::Sell, Amount::Fraction(f)) => {
                let pos = portfolio.positions.get(&intent.mint)?;
                let tokens = pos.tokens * f.clamp(0.0, 1.0);
                let sol = tokens * px * (1.0 - self.cfg.slippage) * (1.0 - self.cfg.fee_rate);
                Some(Fill {
                    mint: intent.mint.clone(),
                    side: Side::Sell,
                    token_amount: tokens,
                    sol_amount: sol,
                    ts_ms,
                })
            }
            _ => None,
        }
    }
}
