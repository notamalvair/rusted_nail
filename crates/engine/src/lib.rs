//! Ядро принятия решений: события -> стратегии -> риск -> намерения.
//! Чистая синхронная логика; исполнением занимается внешний слой и сообщает сюда fill/reject.

#![cfg_attr(test, allow(clippy::panic, clippy::indexing_slicing))]

use std::collections::{BTreeMap, HashMap, HashSet};

use nail_core::{Amount, Event, Fill, Intent, Mint, Portfolio, Side, Wallet};
use nail_risk::RiskManager;
use nail_strategy::{ExitRules, Strategy};

struct Pending {
    strategy: &'static str,
    side: Side,
    sol: f64,
    ts_ms: u64,
}

struct Token {
    creator: Wallet,
    created_ms: u64,
    dev_sold: bool,
}

#[derive(Default, Clone, Debug)]
pub struct StrategyStats {
    pub buys: u64,
    pub sells: u64,
    pub wins: u64,
    pub pnl_sol: f64,
}

/// Сколько помним создателя токена после запуска.
const TOKEN_TTL_MS: u64 = 6 * 3_600_000;

pub struct Engine {
    strategies: Vec<Box<dyn Strategy>>,
    exits: ExitRules,
    pub risk: RiskManager,
    pub portfolio: Portfolio,
    pending: HashMap<Mint, Pending>,
    /// Через сколько считаем неподтверждённый ордер потерянным.
    pub pending_timeout_ms: u64,
    pub rejected: u64,
    /// События, пропущенные из-за признаков ловушки.
    pub risky_skipped: u64,
    pub stats: BTreeMap<&'static str, StrategyStats>,
    tokens: HashMap<Mint, Token>,
    seen: u64,
    /// Стратегии, которым запрещены новые покупки (продажи и выходы работают).
    disabled: HashSet<&'static str>,
}

impl Engine {
    pub fn new(strategies: Vec<Box<dyn Strategy>>, exits: ExitRules, risk: RiskManager) -> Self {
        Self {
            strategies,
            exits,
            risk,
            portfolio: Portfolio::default(),
            pending: HashMap::new(),
            pending_timeout_ms: 15_000,
            rejected: 0,
            risky_skipped: 0,
            stats: BTreeMap::new(),
            tokens: HashMap::new(),
            seen: 0,
            disabled: HashSet::new(),
        }
    }

    /// Имена стратегий и включены ли у них покупки.
    pub fn strategies(&self) -> Vec<(&'static str, bool)> {
        self.strategies
            .iter()
            .map(|s| (s.name(), !self.disabled.contains(s.name())))
            .collect()
    }

    /// Возвращает false, если такой стратегии нет.
    pub fn set_strategy_enabled(&mut self, name: &str, enabled: bool) -> bool {
        let Some(name) = self
            .strategies
            .iter()
            .map(|s| s.name())
            .find(|n| *n == name)
        else {
            return false;
        };
        if enabled {
            self.disabled.remove(name);
        } else {
            self.disabled.insert(name);
        }
        true
    }

    /// Ручное закрытие позиции. `None`, если позиции нет или по ней уже летит ордер.
    pub fn close(&mut self, mint: &str, now_ms: u64) -> Option<Intent> {
        if self.pending.contains_key(mint) {
            return None;
        }
        let pos = self.portfolio.positions.get(mint)?;
        self.pending.insert(
            mint.to_owned(),
            Pending {
                strategy: pos.strategy,
                side: Side::Sell,
                sol: 0.0,
                ts_ms: now_ms,
            },
        );
        Some(Intent {
            strategy: pos.strategy,
            mint: mint.to_owned(),
            side: Side::Sell,
            amount: Amount::Fraction(1.0),
            reason: format!("manual close ({:+.1}%)", pos.pnl_pct() * 100.0),
        })
    }

    pub fn close_all(&mut self, now_ms: u64) -> Vec<Intent> {
        let mints: Vec<Mint> = self.portfolio.positions.keys().cloned().collect();
        mints.iter().filter_map(|m| self.close(m, now_ms)).collect()
    }

    pub fn is_pending(&self, mint: &str) -> bool {
        self.pending.contains_key(mint)
    }

    /// Возвращает намерения, прошедшие риск-проверку. Каждое нужно исполнить и
    /// ответить `on_fill` либо `on_reject`.
    pub fn on_event(&mut self, ev: &Event) -> Vec<Intent> {
        let now = ev.ts_ms();
        let timeout = self.pending_timeout_ms;
        self.pending
            .retain(|_, p| now.saturating_sub(p.ts_ms) < timeout);

        match ev {
            Event::Create(c) => {
                self.tokens.insert(
                    c.mint.clone(),
                    Token {
                        creator: c.creator.clone(),
                        created_ms: now,
                        dev_sold: false,
                    },
                );
            }
            Event::Trade(t) => {
                self.portfolio.mark(&t.mint, t.price);
                if t.side == Side::Sell {
                    if let Some(tok) = self.tokens.get_mut(&t.mint) {
                        tok.dev_sold |= t.involves(&tok.creator);
                    }
                }
            }
            Event::Migrate(_) => {}
        }
        self.seen += 1;
        if self.seen.is_multiple_of(100_000) {
            let positions = &self.portfolio.positions;
            self.tokens.retain(|m, t| {
                now.saturating_sub(t.created_ms) < TOKEN_TTL_MS || positions.contains_key(m)
            });
        }
        let dev_exit = self.exits.exit_on_dev_sell;

        let mut out = Vec::new();

        // Выходы: проверяем все позиции, чтобы таймаут срабатывал и на затихших токенах.
        for (mint, pos) in &self.portfolio.positions {
            if self.pending.contains_key(mint) {
                continue;
            }
            let dev_sold = dev_exit && self.tokens.get(mint).is_some_and(|t| t.dev_sold);
            let reason = if dev_sold {
                Some("dev sold")
            } else {
                self.exits.check(pos, now)
            };
            if let Some(reason) = reason {
                out.push(Intent {
                    strategy: pos.strategy,
                    mint: mint.clone(),
                    side: Side::Sell,
                    amount: Amount::Fraction(1.0),
                    reason: format!("{reason} ({:+.1}%)", pos.pnl_pct() * 100.0),
                });
            }
        }
        for i in &out {
            self.pending.insert(
                i.mint.clone(),
                Pending {
                    strategy: i.strategy,
                    side: Side::Sell,
                    sol: 0.0,
                    ts_ms: now,
                },
            );
        }

        if ev.risky() {
            self.risky_skipped += 1;
            return out;
        }

        // Сначала собираем сигналы: стратегии заимствуют self изменяемо.
        let signals: Vec<Intent> = self
            .strategies
            .iter_mut()
            .filter_map(|s| s.on_event(ev))
            .collect();
        for intent in signals {
            if self.pending.contains_key(&intent.mint) {
                continue;
            }
            let sol = match (intent.side, intent.amount) {
                (Side::Buy, Amount::Sol(sol)) => {
                    if self.disabled.contains(intent.strategy) {
                        continue;
                    }
                    if self.portfolio.positions.contains_key(&intent.mint) {
                        continue; // без доливок
                    }
                    if dev_exit && self.tokens.get(&intent.mint).is_some_and(|t| t.dev_sold) {
                        continue;
                    }
                    let buys = self.pending.values().filter(|p| p.side == Side::Buy);
                    let (n, pend_sol) = buys.fold((0, 0.0), |(n, s), p| (n + 1, s + p.sol));
                    if self
                        .risk
                        .check_buy(
                            sol,
                            self.portfolio.positions.len() + n,
                            self.portfolio.exposure_sol() + pend_sol,
                            now,
                        )
                        .is_err()
                    {
                        self.rejected += 1;
                        continue;
                    }
                    sol
                }
                (Side::Sell, Amount::Fraction(_)) => {
                    match self.portfolio.positions.get(&intent.mint) {
                        Some(p) if p.strategy == intent.strategy => 0.0,
                        _ => continue,
                    }
                }
                _ => continue,
            };
            self.pending.insert(
                intent.mint.clone(),
                Pending {
                    strategy: intent.strategy,
                    side: intent.side,
                    sol,
                    ts_ms: now,
                },
            );
            out.push(intent);
        }
        out
    }

    /// Возвращает реализованный PnL для продаж.
    pub fn on_fill(&mut self, fill: &Fill) -> Option<f64> {
        let strategy = match self.pending.remove(&fill.mint) {
            Some(p) => p.strategy,
            None => self
                .portfolio
                .positions
                .get(&fill.mint)
                .map_or("unknown", |p| p.strategy),
        };
        let pnl = self.portfolio.apply_fill(fill, strategy);
        let s = self.stats.entry(strategy).or_default();
        match pnl {
            Some(pnl) => {
                s.sells += 1;
                s.wins += u64::from(pnl > 0.0);
                s.pnl_sol += pnl;
                self.risk.on_realized(pnl, fill.ts_ms);
            }
            None => s.buys += 1,
        }
        pnl
    }

    pub fn on_reject(&mut self, mint: &str) {
        self.pending.remove(mint);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use nail_core::{Create, Pool, Trade, PUMP_INITIAL_PRICE_SOL as P0};
    use nail_risk::RiskConfig;
    use nail_sim::{PaperExecutor, SimConfig};
    use nail_strategy::{
        CopyTrade, CopyTradeConfig, Momentum, MomentumConfig, Sniper, SniperConfig,
    };

    fn create(mint: &str, creator: &str, ts: u64) -> Event {
        Event::Create(Create {
            signature: String::new(),
            mint: mint.into(),
            pool: Pool::Pump,
            creator: creator.into(),
            name: "n".into(),
            symbol: "S".into(),
            risky: false,
            block: 0,
            ts_ms: ts,
        })
    }

    fn trade(mint: &str, who: &str, side: Side, sol: f64, price: f64, ts: u64) -> Event {
        Event::Trade(Trade {
            signature: String::new(),
            mint: mint.into(),
            pool: Pool::Pump,
            side,
            trader: who.into(),
            traders: vec![who.into()],
            token_amount: sol / price,
            sol_amount: sol,
            price,
            market_cap_sol: price * 1e9,
            risky: false,
            block: 0,
            ts_ms: ts,
        })
    }

    struct Rig {
        engine: Engine,
        sim: PaperExecutor,
        log: Vec<(Side, String)>,
    }

    impl Rig {
        fn new(strategies: Vec<Box<dyn Strategy>>) -> Self {
            Self {
                engine: Engine::new(
                    strategies,
                    ExitRules::default(),
                    RiskManager::new(RiskConfig::default()),
                ),
                sim: PaperExecutor::new(SimConfig::default()),
                log: vec![],
            }
        }
        fn feed(&mut self, ev: Event) {
            self.sim.observe(&ev);
            for i in self.engine.on_event(&ev) {
                match self.sim.fill(&i, &self.engine.portfolio, ev.ts_ms()) {
                    Some(f) => {
                        self.engine.on_fill(&f);
                        self.log.push((i.side, i.reason.clone()));
                    }
                    None => self.engine.on_reject(&i.mint),
                }
            }
        }
    }

    #[test]
    fn sniper_buys_then_takes_profit() {
        let mut r = Rig::new(vec![Box::new(Sniper::new(SniperConfig::default()))]);
        r.feed(create("M", "dev", 1_000));
        assert_eq!(r.engine.portfolio.positions.len(), 1);
        r.feed(trade("M", "x", Side::Buy, 1.0, P0 * 2.0, 2_000));
        assert!(r.engine.portfolio.positions.is_empty());
        assert!(r.log[1].1.starts_with("take profit"));
        assert!(r.engine.portfolio.realized_pnl_sol > 0.0);
    }

    #[test]
    fn sniper_skips_serial_deployer_and_stops_out() {
        let mut r = Rig::new(vec![Box::new(Sniper::new(SniperConfig::default()))]);
        r.feed(create("A", "dev", 1_000));
        r.feed(create("B", "dev", 1_500));
        assert_eq!(r.engine.portfolio.positions.len(), 1);
        r.feed(trade("A", "x", Side::Sell, 1.0, P0 * 0.5, 2_000));
        assert!(r.log[1].1.starts_with("stop loss"));
        assert!(r.engine.portfolio.realized_pnl_sol < 0.0);
    }

    #[test]
    fn dev_sell_triggers_exit_and_blocks_reentry() {
        let cfg = SniperConfig {
            max_launches_per_creator: 9,
            ..Default::default()
        };
        let mut r = Rig::new(vec![Box::new(Sniper::new(cfg))]);
        r.feed(create("A", "dev", 1_000));
        r.feed(trade("A", "dev", Side::Sell, 1.0, P0, 1_500));
        assert!(r.engine.portfolio.positions.is_empty());
        assert!(r.log[1].1.starts_with("dev sold"));
        assert_eq!(r.engine.stats["sniper"].sells, 1);
    }

    #[test]
    fn risky_events_never_reach_strategies() {
        let mut r = Rig::new(vec![Box::new(Sniper::new(SniperConfig::default()))]);
        let Event::Create(mut c) = create("A", "dev", 1_000) else {
            return;
        };
        c.risky = true;
        r.feed(Event::Create(c));
        assert!(r.engine.portfolio.positions.is_empty());
        assert_eq!(r.engine.risky_skipped, 1);
    }

    #[test]
    fn disabled_strategy_stops_buying_and_manual_close_works() {
        let cfg = SniperConfig {
            max_launches_per_creator: 9,
            ..Default::default()
        };
        let mut r = Rig::new(vec![Box::new(Sniper::new(cfg))]);
        r.feed(create("A", "dev", 1_000));
        assert!(r.engine.set_strategy_enabled("sniper", false));
        assert!(!r.engine.set_strategy_enabled("nope", false));
        r.feed(create("B", "dev", 1_100));
        assert_eq!(r.engine.portfolio.positions.len(), 1);

        let intents = r.engine.close_all(1_200);
        assert_eq!(intents.len(), 1);
        assert!(r.engine.close("A", 1_200).is_none(), "ордер уже в полёте");
        let fill = r.sim.fill(&intents[0], &r.engine.portfolio, 1_200);
        assert!(fill.is_some_and(|f| r.engine.on_fill(&f).is_some()));
        assert!(r.engine.portfolio.positions.is_empty());
    }

    #[test]
    fn timeout_exit_fires_on_unrelated_event() {
        let mut r = Rig::new(vec![Box::new(Sniper::new(SniperConfig::default()))]);
        r.feed(create("A", "dev", 1_000));
        r.feed(trade("Z", "x", Side::Buy, 0.1, P0, 200_000));
        assert!(r.engine.portfolio.positions.is_empty());
        assert!(r.log[1].1.starts_with("max hold time"));
    }

    #[test]
    fn momentum_needs_volume_and_unique_buyers() {
        let mut r = Rig::new(vec![Box::new(Momentum::new(MomentumConfig::default()))]);
        let px = 50.0 / 1e9; // mcap 50 SOL
        for i in 0..4 {
            r.feed(trade("M", "same", Side::Buy, 1.0, px, 1_000 + i));
        }
        assert!(
            r.engine.portfolio.positions.is_empty(),
            "один покупатель — не сигнал"
        );
        for i in 0..4 {
            r.feed(trade("M", &format!("w{i}"), Side::Buy, 0.1, px, 1_100 + i));
        }
        assert_eq!(r.engine.portfolio.positions["M"].strategy, "momentum");
    }

    #[test]
    fn copytrade_follows_buy_and_sell() {
        let cfg = CopyTradeConfig {
            size_sol: 0.03,
            wallets: ["whale".to_string()].into(),
        };
        let mut r = Rig::new(vec![Box::new(CopyTrade::new(cfg))]);
        r.feed(trade("M", "nobody", Side::Buy, 1.0, P0, 1_000));
        assert!(r.engine.portfolio.positions.is_empty());
        r.feed(trade("M", "whale", Side::Buy, 1.0, P0, 1_100));
        assert_eq!(r.engine.portfolio.positions.len(), 1);
        r.feed(trade("M", "whale", Side::Sell, 1.0, P0, 1_200));
        assert!(r.engine.portfolio.positions.is_empty());
    }

    #[test]
    fn risk_caps_open_positions_and_pending_blocks_duplicates() {
        let cfg = SniperConfig {
            max_launches_per_creator: 100,
            ..Default::default()
        };
        let mut e = Engine::new(
            vec![Box::new(Sniper::new(cfg))],
            ExitRules::default(),
            RiskManager::new(RiskConfig::default()),
        );
        // Ничего не исполняем: все ордера остаются в полёте и считаются в лимитах.
        let n: usize = (0..10)
            .map(|i| e.on_event(&create(&format!("M{i}"), "d", 1_000 + i)).len())
            .sum();
        assert_eq!(n, 5);
        assert_eq!(e.rejected, 5);
        assert!(e.is_pending("M0"));
    }
}
