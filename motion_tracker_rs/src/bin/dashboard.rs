use axum::{
    extract::{
        ws::{Message, WebSocket, WebSocketUpgrade},
        State,
    },
    response::{Html, IntoResponse},
    routing::get,
    Router,
};
use clap::Parser;
use futures::{sink::SinkExt, stream::StreamExt};
use serde::{Deserialize, Serialize};
use std::{net::SocketAddr, path::PathBuf, sync::Arc, time::Duration};
use tokio::net::TcpListener;
use tokio::time::sleep;

#[derive(Parser, Debug)]
#[command(name = "dashboard")]
struct Args {
    /// Path to motion tracker output directory
    #[arg(long, default_value = "motion_tracker_sessions")]
    data_dir: PathBuf,

    /// Port to serve on
    #[arg(long, default_value = "8081")]
    port: u16,
}

#[derive(Clone)]
struct AppState {
    data_dir: PathBuf,
}

#[tokio::main]
async fn main() {
    let args = Args::parse();
    
    if !args.data_dir.exists() {
        eprintln!("Warning: Data directory {:?} does not exist", args.data_dir);
    }

    let state = AppState {
        data_dir: args.data_dir.clone(),
    };

    let app = Router::new()
        .route("/", get(index_handler))
        .route("/ws", get(ws_handler))
        .with_state(state);

    let addr = SocketAddr::from(([0, 0, 0, 0], args.port));
    println!("Standalone Dashboard listening on http://{}", addr);
    println!("Watching directory: {:?}", args.data_dir);

    let listener = TcpListener::bind(addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}

async fn index_handler() -> Html<&'static str> {
    // Reuse the same HTML template, we might want to inject a "Standalone" badge later
    Html(include_str!("../dashboard_static.html"))
}

async fn ws_handler(
    ws: WebSocketUpgrade,
    State(state): State<AppState>,
) -> impl IntoResponse {
    ws.on_upgrade(|socket| handle_socket(socket, state))
}

#[derive(Deserialize, Serialize, Default)]
struct LiveStatus {
    timestamp: f64,
    accel_samples: u64,
    gyro_samples: u64,
    gps_fixes: u64,
    gps_speed: Option<f64>,
    gps_bearing: Option<f64>,
    gravity_magnitude: Option<f64>,
    uptime_seconds: u64,
    // Add specific fields mapped to dashboard expectations
    #[serde(default)]
    accel_x: f64,
    #[serde(default)]
    accel_y: f64,
    #[serde(default)]
    accel_z: f64,
}

async fn handle_socket(mut socket: WebSocket, state: AppState) {
    let status_file = state.data_dir.join("live_status.json");
    let mut last_mtime = std::time::SystemTime::UNIX_EPOCH;

    loop {
        // Poll file for changes
        if let Ok(metadata) = std::fs::metadata(&status_file) {
            if let Ok(mtime) = metadata.modified() {
                if mtime > last_mtime {
                    last_mtime = mtime;
                    
                    if let Ok(content) = tokio::fs::read_to_string(&status_file).await {
                        // Forward the JSON directly, or parse/enrich it
                        // For now, let's verify it parses, then send
                        if let Ok(mut status) = serde_json::from_str::<LiveStatus>(&content) {
                            // Fill in gaps if needed, but the Rust tracker writes full objects
                            // The embedded dashboard expects specific fields:
                            // uptime, accel_samples, gps_speed, etc.
                            // Our LiveStatus struct matches the file format.
                            
                            let json = serde_json::to_string(&status).unwrap();
                            if socket.send(Message::Text(json)).await.is_err() {
                                break;
                            }
                        }
                    }
                }
            }
        }

        // 2Hz polling (matches tracker write rate)
        sleep(Duration::from_millis(500)).await;
    }
}
