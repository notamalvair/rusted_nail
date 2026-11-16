//! Стратегии: чистая логика без I/O. Один и тот же код для paper, бэктеста и боевого режима.
//! Параметры по умолчанию — заглушки, настоящие подберём по историческим данным.

mod copytrade;
mod exits;
mod momentum;
mod sniper;

pub use copytrade::{CopyTrade, CopyTradeConfig};
pub use exits::ExitRules;
pub use momentum::{Momentum, MomentumConfig};
pub use sniper::{Sniper, SniperConfig};

use nail_core::{Event, Intent};

pub trait Strategy: Send {
    fn name(&self) -> &'static str;
    fn on_event(&mut self, ev: &Event) -> Option<Intent>;
}
