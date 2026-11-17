//! Риск-лимиты. Проверяются перед каждой покупкой; продажи не блокируются никогда.

use std::fmt;

#[derive(Clone, Debug)]
pub struct RiskConfig {
    pub max_position_sol: f64,
    pub max_open_positions: usize,
    pub max_exposure_sol: f64,
    /// Дневной убыток (UTC), после которого покупки останавливаются до следующего дня.
    pub daily_loss_limit_sol: f64,
    /// Не входить в пулы с комиссией выше (доля). На кривой pump.fun обычно 1.25%, бывает до ~4%.
    pub max_pool_fee: f64,
}

impl Default for RiskConfig {
    fn default() -> Self {
        Self {
            max_position_sol: 0.05,
            max_open_positions: 5,
            max_exposure_sol: 0.25,
            daily_loss_limit_sol: 0.2,
            max_pool_fee: 0.0125,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Reject {
    KillSwitch,
    DailyLossLimit,
    PositionTooLarge,
    TooManyPositions,
    ExposureLimit,
    BadSize,
}

impl fmt::Display for Reject {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt::Debug::fmt(self, f)
    }
}

pub struct RiskManager {
    cfg: RiskConfig,
    day: u64,
    day_pnl_sol: f64,
    kill_switch: bool,
}

const DAY_MS: u64 = 86_400_000;

impl RiskManager {
    pub fn new(cfg: RiskConfig) -> Self {
        Self {
            cfg,
            day: 0,
            day_pnl_sol: 0.0,
            kill_switch: false,
        }
    }

    fn roll(&mut self, now_ms: u64) {
        let day = now_ms / DAY_MS;
        if day != self.day {
            self.day = day;
            self.day_pnl_sol = 0.0;
        }
    }

    /// Ручной аварийный стоп: покупки запрещены, пока не снят.
    pub fn set_kill_switch(&mut self, on: bool) {
        self.kill_switch = on;
    }

    pub fn max_pool_fee(&self) -> f64 {
        self.cfg.max_pool_fee
    }

    pub fn kill_switch(&self) -> bool {
        self.kill_switch
    }

    pub fn day_pnl_sol(&self) -> f64 {
        self.day_pnl_sol
    }

    pub fn halted(&self) -> bool {
        self.kill_switch || self.day_pnl_sol <= -self.cfg.daily_loss_limit_sol
    }

    /// `open_positions` и `exposure_sol` включают ордера в полёте.
    pub fn check_buy(
        &mut self,
        size_sol: f64,
        open_positions: usize,
        exposure_sol: f64,
        now_ms: u64,
    ) -> Result<(), Reject> {
        self.roll(now_ms);
        if self.kill_switch {
            return Err(Reject::KillSwitch);
        }
        if self.day_pnl_sol <= -self.cfg.daily_loss_limit_sol {
            return Err(Reject::DailyLossLimit);
        }
        if !(size_sol.is_finite() && size_sol > 0.0) {
            return Err(Reject::BadSize);
        }
        if size_sol > self.cfg.max_position_sol {
            return Err(Reject::PositionTooLarge);
        }
        if open_positions >= self.cfg.max_open_positions {
            return Err(Reject::TooManyPositions);
        }
        if exposure_sol + size_sol > self.cfg.max_exposure_sol {
            return Err(Reject::ExposureLimit);
        }
        Ok(())
    }

    pub fn on_realized(&mut self, pnl_sol: f64, now_ms: u64) {
        self.roll(now_ms);
        self.day_pnl_sol += pnl_sol;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn limits() {
        let mut r = RiskManager::new(RiskConfig::default());
        assert_eq!(r.check_buy(0.05, 0, 0.0, 1), Ok(()));
        assert_eq!(r.check_buy(0.06, 0, 0.0, 1), Err(Reject::PositionTooLarge));
        assert_eq!(r.check_buy(0.05, 5, 0.0, 1), Err(Reject::TooManyPositions));
        assert_eq!(r.check_buy(0.05, 1, 0.21, 1), Err(Reject::ExposureLimit));
        assert_eq!(r.check_buy(f64::NAN, 0, 0.0, 1), Err(Reject::BadSize));
    }

    #[test]
    fn daily_loss_halts_until_next_day() {
        let mut r = RiskManager::new(RiskConfig::default());
        r.on_realized(-0.2, 1);
        assert_eq!(r.check_buy(0.01, 0, 0.0, 2), Err(Reject::DailyLossLimit));
        assert_eq!(r.check_buy(0.01, 0, 0.0, DAY_MS + 1), Ok(()));
    }

    #[test]
    fn kill_switch() {
        let mut r = RiskManager::new(RiskConfig::default());
        r.set_kill_switch(true);
        assert_eq!(r.check_buy(0.01, 0, 0.0, 1), Err(Reject::KillSwitch));
    }
}
