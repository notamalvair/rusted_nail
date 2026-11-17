#![cfg_attr(test, allow(clippy::indexing_slicing))]
//! Paper-исполнение: fill по цене через `latency_ms` после сигнала, с комиссией и проскальзыванием.
//! Влияние собственной сделки на цену не моделируется (при малых размерах оно доли процента).

mod race;

pub use race::RaceStats;

use std::collections::{HashMap, VecDeque};

use nail_core::{Amount, Event, Fill, Intent, Mint, Portfolio, Side, PUMP_INITIAL_PRICE_SOL};

#[derive(Clone, Debug)]
pub struct SimConfig {
    /// Комиссия pumpapi с оборота (0.25%). Комиссия пула берётся из событий токена.
    pub api_fee_rate: f64,
    /// Комиссия пула, если в событиях её нет (на кривой pump.fun обычно 1.25%).
    pub default_pool_fee: f64,
    /// Проскальзывание в худшую сторону, доля.
    pub slippage: f64,
    /// Задержка от сигнала до исполнения. 0 = мгновенно (слишком оптимистично).
    pub latency_ms: u64,
}

impl Default for SimConfig {
    fn default() -> Self {
        Self {
            api_fee_rate: 0.0025,
            default_pool_fee: 0.0125,
            slippage: 0.03,
            latency_ms: 400,
        }
    }
}

pub struct PaperExecutor {
    cfg: SimConfig,
    /// Последние цена и комиссия пула по токену.
    last: HashMap<Mint, (f64, f64)>,
    queue: VecDeque<(u64, Intent)>,
}

impl PaperExecutor {
    pub fn new(cfg: SimConfig) -> Self {
        Self {
            cfg,
            last: HashMap::new(),
            queue: VecDeque::new(),
        }
    }

    /// Вызывать на каждое событие до `fill`.
    pub fn observe(&mut self, ev: &Event) {
        let fee = |f: f64| {
            if f > 0.0 {
                f
            } else {
                self.cfg.default_pool_fee
            }
        };
        match ev {
            Event::Trade(t) if t.price > 0.0 => {
                self.last.insert(t.mint.clone(), (t.price, fee(t.pool_fee)));
            }
            Event::Create(c) => {
                let price = if c.price > 0.0 {
                    c.price
                } else {
                    PUMP_INITIAL_PRICE_SOL
                };
                self.last
                    .entry(c.mint.clone())
                    .or_insert((price, fee(c.pool_fee)));
            }
            _ => {}
        }
    }

    /// Ставит ордер в очередь; исполнится в `poll`, когда пройдёт задержка.
    pub fn submit(&mut self, intent: Intent, now_ms: u64) {
        self.queue.push_back((now_ms + self.cfg.latency_ms, intent));
    }

    /// Исполняет созревшие ордера. `None` = исполнить не удалось (нет цены или позиции).
    pub fn poll(&mut self, now_ms: u64, portfolio: &Portfolio) -> Vec<(Intent, Option<Fill>)> {
        let mut out = Vec::new();
        while let Some((_, intent)) = self.queue.pop_front_if(|(due, _)| *due <= now_ms) {
            let fill = self.fill(&intent, portfolio, now_ms);
            out.push((intent, fill));
        }
        out
    }

    pub fn fill(&self, intent: &Intent, portfolio: &Portfolio, ts_ms: u64) -> Option<Fill> {
        let (px, pool_fee) = *self.last.get(&intent.mint)?;
        let fee = pool_fee + self.cfg.api_fee_rate;
        match (intent.side, intent.amount) {
            (Side::Buy, Amount::Sol(sol)) => {
                let tokens = sol * (1.0 - fee) / (px * (1.0 + self.cfg.slippage));
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
                let sol = tokens * px * (1.0 - self.cfg.slippage) * (1.0 - fee);
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

#[cfg(test)]
mod tests {
    use super::*;
    use nail_core::{Create, Pool, Trade};

    fn create(block: u64) -> Event {
        Event::Create(Create {
            signature: String::new(),
            mint: "M".into(),
            pool: Pool::Pump,
            creator: "dev".into(),
            name: String::new(),
            symbol: String::new(),
            sol_quote: true,
            price: 0.0,
            pool_fee: 0.0,
            dev_buy_sol: 0.0,
            risky: false,
            block,
            ts_ms: 0,
        })
    }

    fn buy(who: &str, price: f64, block: u64) -> Event {
        Event::Trade(Trade {
            signature: String::new(),
            mint: "M".into(),
            pool: Pool::Pump,
            side: Side::Buy,
            trader: who.into(),
            traders: vec![],
            token_amount: 1.0,
            sol_amount: 2.0,
            price,
            market_cap_sol: 0.0,
            sol_quote: true,
            pool_fee: 0.0,
            curve_sol: 0.0,
            risky: false,
            block,
            ts_ms: 0,
        })
    }

    #[test]
    fn order_fills_after_latency_at_the_later_price() {
        let mut sim = PaperExecutor::new(SimConfig {
            api_fee_rate: 0.0,
            default_pool_fee: 0.0,
            slippage: 0.0,
            latency_ms: 400,
        });
        let pf = Portfolio::default();
        sim.observe(&buy("x", 1.0, 1));
        let intent = Intent {
            strategy: "t",
            mint: "M".into(),
            side: Side::Buy,
            amount: Amount::Sol(1.0),
            reason: String::new(),
        };
        sim.submit(intent, 1_000);
        assert!(sim.poll(1_399, &pf).is_empty());
        sim.observe(&buy("x", 2.0, 2));
        let fills = sim.poll(1_400, &pf);
        assert_eq!(fills[0].1.as_ref().map(|f| f.token_amount), Some(0.5));
    }

    #[test]
    fn race_buckets_skip_dev_buy() {
        let mut r = RaceStats::default();
        r.observe(&create(100));
        r.observe(&buy("dev", 1.0, 100));
        r.observe(&buy("a", 1.0, 100));
        r.observe(&buy("b", 1.0, 101));
        r.observe(&buy("c", 1.0, 104));
        r.observe(&buy("d", 1.0, 500));
        assert_eq!(r.buckets.map(|b| b.0), [1, 1, 0, 1, 0]);
        assert_eq!(r.buckets[0].1, 2.0);
    }
}
