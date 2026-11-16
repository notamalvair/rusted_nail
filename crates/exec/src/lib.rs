//! Боевое исполнение через Trade API pumpapi (lightning: сервер сам подписывает и отправляет).

use std::time::{Duration, Instant};

use anyhow::{bail, Context, Result};
use nail_core::{Amount, Intent, Side};
use serde::Deserialize;
use serde_json::json;

pub const DEFAULT_URL: &str = "https://api.pumpapi.io";

#[derive(Clone, Debug)]
pub struct ExecConfig {
    pub url: String,
    /// Проценты, как в API (20 = 20%).
    pub slippage: f64,
    /// SOL. От 0.00023 API сам делит на Jito-чаевые и priority fee.
    pub priority_fee: f64,
}

impl Default for ExecConfig {
    fn default() -> Self {
        Self {
            url: DEFAULT_URL.into(),
            slippage: 20.0,
            priority_fee: 0.0001,
        }
    }
}

#[derive(Deserialize)]
struct TradeResponse {
    #[serde(default)]
    signature: String,
    #[serde(default)]
    err: String,
}

pub struct Sent {
    pub signature: String,
    /// Время до ответа сервера.
    pub latency: Duration,
}

pub struct PumpApiClient {
    http: reqwest::Client,
    api_key: String,
    cfg: ExecConfig,
}

impl PumpApiClient {
    pub fn new(api_key: String, cfg: ExecConfig) -> Result<Self> {
        let http = reqwest::Client::builder()
            .tcp_nodelay(true)
            .pool_idle_timeout(Duration::from_secs(300))
            .tcp_keepalive(Duration::from_secs(30))
            .timeout(Duration::from_secs(5))
            .build()
            .context("http client")?;
        Ok(Self { http, api_key, cfg })
    }

    /// Держит TCP/TLS-соединение тёплым, чтобы сделка не платила за рукопожатие.
    /// Ответ не важен: достаточно самого запроса.
    pub async fn warm(&self) {
        let _ = self.http.get(&self.cfg.url).send().await;
    }

    pub async fn trade(&self, intent: &Intent) -> Result<Sent> {
        let (action, amount, in_quote) = match (intent.side, intent.amount) {
            (Side::Buy, Amount::Sol(sol)) => ("buy", json!(sol), "true"),
            (Side::Sell, Amount::Fraction(f)) => (
                "sell",
                json!(format!("{:.0}%", (f * 100.0).clamp(1.0, 100.0))),
                "false",
            ),
            _ => bail!("unsupported side/amount combination"),
        };
        let body = json!({
            "apiKey": self.api_key,
            "action": action,
            "mint": intent.mint,
            "amount": amount,
            "denominatedInQuote": in_quote,
            "slippage": self.cfg.slippage,
            "priorityFee": self.cfg.priority_fee,
        });
        let started = Instant::now();
        let resp = self
            .http
            .post(&self.cfg.url)
            .json(&body)
            .send()
            .await
            .context("send")?;
        let latency = started.elapsed();
        let status = resp.status();
        let text = resp.text().await.context("read body")?;
        let parsed: TradeResponse = serde_json::from_str(&text)
            .with_context(|| format!("unexpected response ({status}): {text}"))?;
        if !parsed.err.is_empty() || parsed.signature.is_empty() {
            bail!("trade rejected ({status}): {text}");
        }
        Ok(Sent {
            signature: parsed.signature,
            latency,
        })
    }
}
