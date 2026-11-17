//! replay [--config config.toml] FILE.jsonl[.zst]...
//! Прогоняет архивы стрима (replay.pumpapi.io) через те же стратегии в paper-режиме.

use std::fs::File;
use std::io::{BufRead, BufReader};
use std::time::Instant;

use anyhow::{bail, Context, Result};
use nail_bot::{config, paper_step};
use nail_sim::{PaperExecutor, RaceStats};

fn open(path: &str) -> Result<Box<dyn BufRead>> {
    let file = File::open(path).with_context(|| format!("open {path}"))?;
    Ok(if path.ends_with(".zst") {
        let dec = zstd::stream::read::Decoder::new(file).context("zstd")?;
        Box::new(BufReader::with_capacity(1 << 20, dec))
    } else {
        Box::new(BufReader::with_capacity(1 << 20, file))
    })
}

fn main() -> Result<()> {
    let mut cfg_path = "config.toml".to_string();
    let mut files = Vec::new();
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        if a == "--config" {
            cfg_path = args.next().context("--config needs a path")?;
        } else {
            files.push(a);
        }
    }
    if files.is_empty() {
        bail!("usage: replay [--config config.toml] FILE.jsonl[.zst]...");
    }
    files.sort();

    let cfg = config::load(&cfg_path)?;
    let mut engine = cfg.engine();
    let mut sim = PaperExecutor::new(cfg.sim());
    let mut race = RaceStats::default();

    let started = Instant::now();
    let (mut lines, mut events) = (0u64, 0u64);
    let (mut first_ts, mut last_ts) = (u64::MAX, 0u64);
    let mut line = Vec::with_capacity(8192);
    let mut parsed = Vec::new();

    for path in &files {
        let mut reader = open(path)?;
        loop {
            line.clear();
            if reader
                .read_until(b'\n', &mut line)
                .with_context(|| format!("read {path}"))?
                == 0
            {
                break;
            }
            lines += 1;
            nail_feed::parse(line.trim_ascii(), &mut parsed);
            for ev in parsed.drain(..) {
                events += 1;
                first_ts = first_ts.min(ev.ts_ms());
                last_ts = last_ts.max(ev.ts_ms());
                race.observe(&ev);
                paper_step(&mut engine, &mut sim, &ev, |_, _, _| {});
            }
        }
        eprintln!("[replay] {path}: {lines} lines so far");
    }

    let hours = last_ts.saturating_sub(first_ts.min(last_ts)) as f64 / 3_600_000.0;
    println!("== replay: {} files, {lines} lines, {events} pump events, {hours:.2} h of data, {:.1} s ==",
        files.len(), started.elapsed().as_secs_f64());
    println!(
        "risky events skipped: {}   high-fee events skipped: {}   risk rejects: {}",
        engine.risky_skipped, engine.fee_skipped, engine.rejected
    );

    println!(
        "\n{:<10} {:>6} {:>6} {:>7} {:>12}",
        "strategy", "buys", "sells", "win %", "pnl SOL"
    );
    for (name, s) in &engine.stats {
        let win = if s.sells > 0 {
            100.0 * s.wins as f64 / s.sells as f64
        } else {
            0.0
        };
        println!(
            "{name:<10} {:>6} {:>6} {win:>7.1} {:>+12.5}",
            s.buys, s.sells, s.pnl_sol
        );
    }
    let pf = &engine.portfolio;
    let unrealized: f64 = pf
        .positions
        .values()
        .map(|p| p.tokens * p.last_price - p.cost_sol)
        .sum();
    println!(
        "realized {:+.5} SOL, open positions {} (unrealized {:+.5} SOL before exit costs)",
        pf.realized_pnl_sol,
        pf.positions.len(),
        unrealized
    );

    println!(
        "\nsniper race over {} launches (dev buys excluded):",
        race.launches
    );
    for (label, (n, sol)) in RaceStats::LABELS.iter().zip(race.buckets) {
        println!("  {label:<10} {n:>8} buys {sol:>12.2} SOL");
    }
    Ok(())
}
