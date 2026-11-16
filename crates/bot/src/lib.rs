//! Общее для бинарников: конфиг и шаг paper-торговли.

pub mod config;
pub mod ui;

use nail_core::{Event, Fill, Intent};
use nail_engine::Engine;
use nail_sim::PaperExecutor;

/// Один шаг paper-режима: событие -> решения -> виртуальное исполнение с задержкой.
/// `on_fill` получает намерение, исполнение и реализованный PnL (для продаж).
pub fn paper_step(
    engine: &mut Engine,
    sim: &mut PaperExecutor,
    ev: &Event,
    mut on_fill: impl FnMut(&Intent, &Fill, Option<f64>),
) {
    let now = ev.ts_ms();
    sim.observe(ev);
    let mut settle = |engine: &mut Engine, sim: &mut PaperExecutor| {
        for (intent, fill) in sim.poll(now, &engine.portfolio) {
            match fill {
                Some(fill) => {
                    let pnl = engine.on_fill(&fill);
                    on_fill(&intent, &fill, pnl);
                }
                None => engine.on_reject(&intent.mint),
            }
        }
    };
    // Сначала ордера, созревшие к этому моменту, потом новые решения.
    settle(engine, sim);
    for intent in engine.on_event(ev) {
        sim.submit(intent, now);
    }
    settle(engine, sim); // сработает только при latency_ms = 0
}
