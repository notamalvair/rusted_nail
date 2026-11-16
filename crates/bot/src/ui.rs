//! Локальная панель управления: страница с кнопками и JSON API.
//! Слушает только loopback. Команды уходят в главный цикл через канал, состояние
//! читается из готового снимка, так что панель не трогает горячий путь.

use std::net::SocketAddr;

use anyhow::{bail, Context, Result};
use axum::extract::{Path, State};
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::{Html, IntoResponse};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::{Deserialize, Serialize};
use tokio::sync::{mpsc, watch};

pub enum Command {
    /// Аварийный стоп покупок.
    Kill(bool),
    Strategy {
        name: String,
        enabled: bool,
    },
    Close(String),
    CloseAll,
}

#[derive(Serialize, Default)]
pub struct Snapshot {
    pub live: bool,
    pub events: u64,
    pub lag_ms: u64,
    pub kill_switch: bool,
    pub halted: bool,
    pub day_pnl_sol: f64,
    pub realized_sol: f64,
    pub exposure_sol: f64,
    pub risk_rejects: u64,
    pub risky_skipped: u64,
    pub strategies: Vec<StrategyRow>,
    pub positions: Vec<PositionRow>,
    pub fills: Vec<FillRow>,
}

#[derive(Serialize)]
pub struct StrategyRow {
    pub name: &'static str,
    pub enabled: bool,
    pub buys: u64,
    pub sells: u64,
    pub wins: u64,
    pub pnl_sol: f64,
}

#[derive(Serialize)]
pub struct PositionRow {
    pub mint: String,
    pub strategy: &'static str,
    pub cost_sol: f64,
    pub pnl_pct: f64,
    pub age_s: u64,
    pub closing: bool,
}

#[derive(Serialize, Clone)]
pub struct FillRow {
    pub ts_ms: u64,
    pub buy: bool,
    pub mint: String,
    pub sol: f64,
    pub pnl_sol: Option<f64>,
    pub reason: String,
}

#[derive(Clone)]
struct App {
    state: watch::Receiver<String>,
    cmd: mpsc::UnboundedSender<Command>,
}

pub async fn serve(
    addr: SocketAddr,
    state: watch::Receiver<String>,
    cmd: mpsc::UnboundedSender<Command>,
) -> Result<()> {
    if !addr.ip().is_loopback() {
        bail!("ui.addr must be a loopback address (use an SSH tunnel for remote access)");
    }
    let app = Router::new()
        .route("/", get(index))
        .route("/api/state", get(get_state))
        .route("/api/kill", post(kill))
        .route("/api/strategy/{name}", post(strategy))
        .route("/api/close/{mint}", post(close))
        .route("/api/close-all", post(close_all))
        .with_state(App { state, cmd });
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .with_context(|| format!("bind {addr}"))?;
    println!("[ui] http://{addr}");
    axum::serve(listener, app).await.context("ui server")
}

/// Защита от чужих страниц в браузере: свой заголовок (чужой сайт не пошлёт его без CORS)
/// и локальный Host (против DNS rebinding).
fn allowed(headers: &HeaderMap) -> bool {
    let local_host = headers
        .get(header::HOST)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|h| {
            h.starts_with("127.0.0.1") || h.starts_with("localhost") || h.starts_with("[::1]")
        });
    local_host && headers.contains_key("x-nail-ui")
}

fn send(app: &App, headers: &HeaderMap, cmd: Command) -> StatusCode {
    if !allowed(headers) {
        StatusCode::FORBIDDEN
    } else if app.cmd.send(cmd).is_err() {
        StatusCode::SERVICE_UNAVAILABLE
    } else {
        StatusCode::NO_CONTENT
    }
}

#[derive(Deserialize)]
struct Toggle {
    on: bool,
}

async fn index() -> Html<&'static str> {
    Html(include_str!("ui.html"))
}

async fn get_state(State(app): State<App>) -> impl IntoResponse {
    let body = app.state.borrow().clone();
    ([(header::CONTENT_TYPE, "application/json")], body)
}

async fn kill(State(app): State<App>, headers: HeaderMap, Json(t): Json<Toggle>) -> StatusCode {
    send(&app, &headers, Command::Kill(t.on))
}

async fn strategy(
    State(app): State<App>,
    Path(name): Path<String>,
    headers: HeaderMap,
    Json(t): Json<Toggle>,
) -> StatusCode {
    send(
        &app,
        &headers,
        Command::Strategy {
            name,
            enabled: t.on,
        },
    )
}

async fn close(State(app): State<App>, Path(mint): Path<String>, headers: HeaderMap) -> StatusCode {
    send(&app, &headers, Command::Close(mint))
}

async fn close_all(State(app): State<App>, headers: HeaderMap) -> StatusCode {
    send(&app, &headers, Command::CloseAll)
}
