//! Общие типы. Без зависимостей и без I/O.

use std::collections::HashMap;

pub type Mint = String;
pub type Wallet = String;

/// Стартовая цена на bonding curve pump.fun (30 виртуальных SOL / 1.073e9 токенов).
pub const PUMP_INITIAL_PRICE_SOL: f64 = 2.795899348462258e-8;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Side {
    Buy,
    Sell,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Pool {
    /// Bonding curve pump.fun.
    Pump,
    /// PumpSwap после миграции.
    PumpAmm,
    Other,
}

#[derive(Clone, Debug)]
pub struct Trade {
    pub signature: String,
    pub mint: Mint,
    pub pool: Pool,
    pub side: Side,
    /// Подписант транзакции.
    pub trader: Wallet,
    /// Адреса, реально исполнившие сделку (tradersInvolved).
    pub traders: Vec<Wallet>,
    pub token_amount: f64,
    /// Объём в SOL.
    pub sol_amount: f64,
    /// SOL за токен после сделки.
    pub price: f64,
    pub market_cap_sol: f64,
    pub block: u64,
    pub ts_ms: u64,
}

impl Trade {
    pub fn involves(&self, wallet: &str) -> bool {
        self.trader == wallet || self.traders.iter().any(|w| w == wallet)
    }
}

#[derive(Clone, Debug)]
pub struct Create {
    pub signature: String,
    pub mint: Mint,
    pub pool: Pool,
    pub creator: Wallet,
    pub name: String,
    pub symbol: String,
    pub block: u64,
    pub ts_ms: u64,
}

#[derive(Clone, Debug)]
pub struct Migrate {
    pub mint: Mint,
    pub block: u64,
    pub ts_ms: u64,
}

#[derive(Clone, Debug)]
pub enum Event {
    Trade(Trade),
    Create(Create),
    Migrate(Migrate),
}

impl Event {
    pub fn ts_ms(&self) -> u64 {
        match self {
            Event::Trade(t) => t.ts_ms,
            Event::Create(c) => c.ts_ms,
            Event::Migrate(m) => m.ts_ms,
        }
    }
    pub fn mint(&self) -> &str {
        match self {
            Event::Trade(t) => &t.mint,
            Event::Create(c) => &c.mint,
            Event::Migrate(m) => &m.mint,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Amount {
    /// Покупка: сколько SOL потратить.
    Sol(f64),
    /// Продажа: доля позиции, 0..=1.
    Fraction(f64),
}

/// Намерение стратегии. Становится ордером только после риск-проверки.
#[derive(Clone, Debug)]
pub struct Intent {
    pub strategy: &'static str,
    pub mint: Mint,
    pub side: Side,
    pub amount: Amount,
    pub reason: String,
}

#[derive(Clone, Debug)]
pub struct Fill {
    pub mint: Mint,
    pub side: Side,
    pub token_amount: f64,
    /// Buy: всего потрачено SOL с комиссиями. Sell: всего получено SOL после комиссий.
    pub sol_amount: f64,
    pub ts_ms: u64,
}

#[derive(Clone, Debug)]
pub struct Position {
    pub strategy: &'static str,
    pub tokens: f64,
    pub cost_sol: f64,
    pub opened_ms: u64,
    pub last_price: f64,
    pub peak_price: f64,
}

impl Position {
    /// Цена входа с учётом комиссий.
    pub fn entry_price(&self) -> f64 {
        self.cost_sol / self.tokens
    }
    pub fn pnl_pct(&self) -> f64 {
        self.last_price / self.entry_price() - 1.0
    }
}

#[derive(Default, Debug)]
pub struct Portfolio {
    pub positions: HashMap<Mint, Position>,
    pub realized_pnl_sol: f64,
}

impl Portfolio {
    pub fn exposure_sol(&self) -> f64 {
        self.positions.values().map(|p| p.cost_sol).sum()
    }

    pub fn mark(&mut self, mint: &str, price: f64) {
        if let Some(p) = self.positions.get_mut(mint) {
            p.last_price = price;
            if price > p.peak_price {
                p.peak_price = price;
            }
        }
    }

    /// Применяет исполнение. Для продаж возвращает реализованный PnL в SOL.
    pub fn apply_fill(&mut self, fill: &Fill, strategy: &'static str) -> Option<f64> {
        if fill.token_amount <= 0.0 {
            return None;
        }
        match fill.side {
            Side::Buy => {
                let px = fill.sol_amount / fill.token_amount;
                let p = self.positions.entry(fill.mint.clone()).or_insert(Position {
                    strategy,
                    tokens: 0.0,
                    cost_sol: 0.0,
                    opened_ms: fill.ts_ms,
                    last_price: px,
                    peak_price: px,
                });
                p.tokens += fill.token_amount;
                p.cost_sol += fill.sol_amount;
                None
            }
            Side::Sell => {
                let p = self.positions.get_mut(&fill.mint)?;
                let frac = (fill.token_amount / p.tokens).min(1.0);
                let cost_part = p.cost_sol * frac;
                let pnl = fill.sol_amount - cost_part;
                p.tokens -= p.tokens * frac;
                p.cost_sol -= cost_part;
                if frac > 0.999_999 {
                    self.positions.remove(&fill.mint);
                }
                self.realized_pnl_sol += pnl;
                Some(pnl)
            }
        }
    }
}
