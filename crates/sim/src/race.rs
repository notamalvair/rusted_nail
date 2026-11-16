use std::collections::HashMap;

use nail_core::{Event, Mint, Pool, Side, Wallet};

/// Гонка снайперов: в каком блоке после создания токена заходят чужие покупки.
/// Покупки самого создателя (dev buy) не считаются.
#[derive(Default)]
pub struct RaceStats {
    created: HashMap<Mint, (u64, Wallet)>,
    pub launches: u64,
    /// (число покупок, объём SOL) по корзинам `LABELS`.
    pub buckets: [(u64, f64); 5],
}

impl RaceStats {
    pub const LABELS: [&'static str; 5] = ["same block", "+1", "+2", "+3..5", "+6..20"];

    pub fn observe(&mut self, ev: &Event) {
        match ev {
            Event::Create(c) if c.pool == Pool::Pump => {
                self.launches += 1;
                self.created
                    .insert(c.mint.clone(), (c.block, c.creator.clone()));
            }
            Event::Trade(t) if t.pool == Pool::Pump && t.side == Side::Buy => {
                let Some((block, creator)) = self.created.get(&t.mint) else {
                    return;
                };
                let idx = match t.block.saturating_sub(*block) {
                    0 => 0,
                    1 => 1,
                    2 => 2,
                    3..=5 => 3,
                    6..=20 => 4,
                    _ => {
                        self.created.remove(&t.mint);
                        return;
                    }
                };
                if t.involves(creator) {
                    return;
                }
                if let Some(b) = self.buckets.get_mut(idx) {
                    b.0 += 1;
                    b.1 += t.sol_amount;
                }
            }
            _ => {}
        }
    }
}
