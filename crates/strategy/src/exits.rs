use nail_core::Position;

/// Правила выхода из позиции. Проценты — доли (0.5 = +50%), считаются от цены входа с комиссиями.
#[derive(Clone, Debug)]
pub struct ExitRules {
    pub take_profit_pct: f64,
    pub stop_loss_pct: f64,
    /// Откат от максимума; работает только когда максимум выше цены входа.
    pub trailing_stop_pct: Option<f64>,
    pub max_hold_ms: u64,
}

impl Default for ExitRules {
    fn default() -> Self {
        Self {
            take_profit_pct: 0.5,
            stop_loss_pct: 0.2,
            trailing_stop_pct: Some(0.25),
            max_hold_ms: 120_000,
        }
    }
}

impl ExitRules {
    pub fn check(&self, p: &Position, now_ms: u64) -> Option<&'static str> {
        let pnl = p.pnl_pct();
        if pnl >= self.take_profit_pct {
            return Some("take profit");
        }
        if pnl <= -self.stop_loss_pct {
            return Some("stop loss");
        }
        if let Some(t) = self.trailing_stop_pct {
            if p.peak_price > p.entry_price() && p.last_price <= p.peak_price * (1.0 - t) {
                return Some("trailing stop");
            }
        }
        if now_ms.saturating_sub(p.opened_ms) >= self.max_hold_ms {
            return Some("max hold time");
        }
        None
    }
}
