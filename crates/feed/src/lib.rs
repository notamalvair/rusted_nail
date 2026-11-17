//! Стрим pumpapi: один WebSocket на IP, без подписок — сервер шлёт всё, фильтруем сами.
//! Формат полей взят из документации; сверить с живым стримом при первом запуске.

#![cfg_attr(test, allow(clippy::panic, clippy::indexing_slicing))]

use std::time::Duration;

use futures_util::StreamExt;
use nail_core::{Create, Event, Migrate, Pool, Side, Trade, WSOL_MINT};
use serde::Deserialize;
use tokio::sync::mpsc;
use tokio_tungstenite::{connect_async, tungstenite::Message};

pub const DEFAULT_URL: &str = "wss://stream.pumpapi.io/";

#[derive(Deserialize, Default)]
#[serde(rename_all = "camelCase", default)]
struct Wire {
    action: String,
    signature: String,
    mint: String,
    pool: String,
    tx_signer: String,
    quote_mint: String,
    pool_fee_rate: Option<f64>,
    quote_in_pool: Option<f64>,
    pool_created_by: Option<String>,
    traders_involved: Option<serde_json::Value>,
    token_amount: Option<f64>,
    quote_amount: Option<f64>,
    price: Option<f64>,
    price_sol: Option<f64>,
    market_cap_sol: Option<f64>,
    name: Option<String>,
    symbol: Option<String>,
    mint_authority: Option<String>,
    freeze_authority: Option<String>,
    token_extensions: Option<serde_json::Value>,
    block: u64,
    timestamp: u64,
}

fn pool(s: &str) -> Pool {
    match s {
        "pump" => Pool::Pump,
        "pump-amm" => Pool::PumpAmm,
        _ => Pool::Other,
    }
}

fn wallets(v: Option<serde_json::Value>) -> Vec<String> {
    match v {
        Some(serde_json::Value::Array(a)) => a
            .into_iter()
            .filter_map(|x| x.as_str().map(str::to_owned))
            .collect(),
        Some(serde_json::Value::Object(o)) => o.into_iter().map(|(k, _)| k).collect(),
        _ => Vec::new(),
    }
}

fn convert(w: Wire) -> Option<Event> {
    let pool = pool(&w.pool);
    if pool == Pool::Other || w.mint.is_empty() {
        return None;
    }
    let risky = w.mint_authority.is_some()
        || w.freeze_authority.is_some()
        || w.token_extensions
            .as_ref()
            .is_some_and(|e| e.get("transferFeeConfig").is_some());
    let sol_quote = w.quote_mint == WSOL_MINT;
    let pool_fee = w.pool_fee_rate.unwrap_or(0.0);
    match w.action.as_str() {
        "buy" | "sell" => {
            let price = w.price_sol.or(w.price)?;
            let token_amount = w.token_amount?;
            // Объём в SOL: для SOL-пулов берём quoteAmount, для остальных пересчёт по priceSol.
            let sol_amount = match (sol_quote, w.quote_amount, w.price_sol) {
                (true, Some(q), _) => q,
                (_, _, Some(p)) => token_amount * p,
                _ => return None,
            };
            let curve_sol = if pool == Pool::Pump && sol_quote {
                w.quote_in_pool.unwrap_or(0.0)
            } else {
                0.0
            };
            Some(Event::Trade(Trade {
                signature: w.signature,
                mint: w.mint,
                pool,
                side: if w.action == "buy" {
                    Side::Buy
                } else {
                    Side::Sell
                },
                trader: w.tx_signer,
                traders: wallets(w.traders_involved),
                token_amount,
                sol_amount,
                price,
                market_cap_sol: w.market_cap_sol.unwrap_or(0.0),
                sol_quote,
                pool_fee,
                curve_sol,
                risky,
                block: w.block,
                ts_ms: w.timestamp,
            }))
        }
        "create" => Some(Event::Create(Create {
            signature: w.signature,
            mint: w.mint,
            pool,
            creator: w.pool_created_by.unwrap_or(w.tx_signer),
            name: w.name.unwrap_or_default(),
            symbol: w.symbol.unwrap_or_default(),
            sol_quote,
            price: w.price_sol.unwrap_or(0.0),
            pool_fee,
            // quoteAmount у события create — покупка создателя в той же транзакции.
            dev_buy_sol: if sol_quote {
                w.quote_amount.unwrap_or(0.0)
            } else {
                0.0
            },
            risky,
            block: w.block,
            ts_ms: w.timestamp,
        })),
        "migrate" => Some(Event::Migrate(Migrate {
            mint: w.mint,
            block: w.block,
            ts_ms: w.timestamp,
        })),
        _ => None,
    }
}

/// Разбирает одно сообщение стрима (объект или массив объектов).
pub fn parse(bytes: &[u8], out: &mut Vec<Event>) {
    if bytes.first() == Some(&b'[') {
        if let Ok(v) = serde_json::from_slice::<Vec<Wire>>(bytes) {
            out.extend(v.into_iter().filter_map(convert));
        }
    } else if let Ok(w) = serde_json::from_slice::<Wire>(bytes) {
        out.extend(convert(w));
    }
}

/// Читает стрим и переподключается при обрыве. Завершается, когда закрыт получатель.
pub async fn run(url: String, tx: mpsc::Sender<Event>) {
    let mut backoff = Duration::from_millis(250);
    let mut buf = Vec::new();
    loop {
        match connect_async(url.as_str()).await {
            Ok((mut ws, _)) => {
                eprintln!("[feed] connected {url}");
                backoff = Duration::from_millis(250);
                while let Some(msg) = ws.next().await {
                    match msg {
                        Ok(Message::Text(t)) => parse(t.as_bytes(), &mut buf),
                        Ok(Message::Binary(b)) => parse(b.as_ref(), &mut buf),
                        Ok(Message::Close(_)) => break,
                        Ok(_) => {}
                        Err(e) => {
                            eprintln!("[feed] error: {e}");
                            break;
                        }
                    }
                    for ev in buf.drain(..) {
                        if tx.send(ev).await.is_err() {
                            return;
                        }
                    }
                }
                eprintln!("[feed] disconnected");
            }
            Err(e) => eprintln!("[feed] connect failed: {e}"),
        }
        tokio::time::sleep(backoff).await;
        backoff = (backoff * 2).min(Duration::from_secs(10));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_buy_and_skips_other_pools() {
        let mut out = Vec::new();
        parse(
            br#"{"signature":"s","action":"buy","mint":"M","pool":"pump","txSigner":"W",
                "quoteMint":"So11111111111111111111111111111111111111112","poolFeeRate":0.0125,
                "tokenAmount":1000.0,"quoteAmount":0.5,"price":0.0005,"priceSol":0.0005,
                "quoteInPool":12.5,"marketCapSol":40.0,"tradersInvolved":["W"],"block":1,
                "timestamp":2,"extra":{}}"#,
            &mut out,
        );
        parse(
            br#"{"action":"buy","mint":"M","pool":"raydium-v4","tokenAmount":1,"price":1}"#,
            &mut out,
        );
        parse(
            br#"{"action":"transfer","txSigner":"W","transfers":[]}"#,
            &mut out,
        );
        assert_eq!(out.len(), 1);
        let Event::Trade(t) = &out[0] else { panic!() };
        assert_eq!((t.side, t.sol_amount, t.ts_ms), (Side::Buy, 0.5, 2));
        assert!(t.involves("W") && !t.risky && t.sol_quote);
        assert_eq!((t.pool_fee, t.curve_sol), (0.0125, 12.5));

        out.clear();
        parse(
            br#"{"action":"create","mint":"M","pool":"pump","txSigner":"D","freezeAuthority":"F",
                "mintAuthority":null,"block":1,"timestamp":2}"#,
            &mut out,
        );
        assert!(out[0].risky());
    }
}
