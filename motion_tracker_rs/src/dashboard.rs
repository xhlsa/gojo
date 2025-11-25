use axum::{
    extract::{
        ws::{Message, WebSocket, WebSocketUpgrade},
        State,
    },
    response::{Html, IntoResponse},
    routing::get,
    Router,
};
use serde::Serialize;
use std::time::Duration;
use tokio::net::TcpListener;
use tokio::time::sleep;

use crate::SensorState;

#[derive(Serialize)]
struct DashboardMetrics {
    uptime: u64,
    accel_samples: u64,
    gyro_samples: u64,
    gps_fixes: u64,
    gps_speed: f64,
    gps_bearing: f64,
    gps_lat: f64,
    gps_lon: f64,
    accel_x: f64,
    accel_y: f64,
    accel_z: f64,
    specific_power_w_per_kg: f64,
    power_coefficient: f64,
}

pub async fn start_dashboard(sensor_state: SensorState, port: u16) {
    let app = Router::new()
        .route("/", get(index_handler))
        .route("/ws", get(ws_handler))
        .with_state(sensor_state);

    let addr = format!("0.0.0.0:{}", port);
    eprintln!("[DASHBOARD] Starting embedded server at http://{}", addr);

    let listener = TcpListener::bind(&addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}

async fn index_handler() -> Html<&'static str> {
    Html(include_str!("dashboard_static.html"))
}

async fn ws_handler(ws: WebSocketUpgrade, State(state): State<SensorState>) -> impl IntoResponse {
    ws.on_upgrade(|socket| handle_socket(socket, state))
}

async fn handle_socket(mut socket: WebSocket, state: SensorState) {
    let start_time = std::time::Instant::now(); // Local session uptime for now

    // Heartbeat / Push loop
    loop {
        // Snapshot metrics
        let metrics = {
            let accel_count = *state.accel_count.read().await;
            let gyro_count = *state.gyro_count.read().await;
            let gps_count = *state.gps_count.read().await;

            let gps_data = state.latest_gps.read().await;
            let (speed, bearing, lat, lon) = if let Some(g) = gps_data.as_ref() {
                (g.speed, g.bearing, g.latitude, g.longitude)
            } else {
                (0.0, 0.0, 0.0, 0.0)
            };

            let accel_data = state.latest_accel.read().await;
            let (ax, ay, az) = if let Some(a) = accel_data.as_ref() {
                (a.x, a.y, a.z)
            } else {
                (0.0, 0.0, 0.0)
            };

            // Calculate specific power (vehicle-agnostic metric) using available speed
            let calc_velocity = if speed > 0.1 { speed } else { 0.0 };
            let (sp_w_kg, pc) = if calc_velocity > 0.0 && (ax != 0.0 || ay != 0.0 || az != 0.0) {
                use crate::physics;
                let power = physics::calculate_specific_power(ax, ay, az, calc_velocity);
                (
                    (power.specific_power_w_per_kg * 100.0).round() / 100.0,
                    (power.power_coefficient * 100.0).round() / 100.0,
                )
            } else {
                (0.0, 0.0)
            };

            DashboardMetrics {
                uptime: start_time.elapsed().as_secs(),
                accel_samples: accel_count,
                gyro_samples: gyro_count,
                gps_fixes: gps_count,
                gps_speed: speed,
                gps_bearing: bearing,
                gps_lat: lat,
                gps_lon: lon,
                accel_x: ax,
                accel_y: ay,
                accel_z: az,
                specific_power_w_per_kg: sp_w_kg,
                power_coefficient: pc,
            }
        };

        let json = serde_json::to_string(&metrics).unwrap();
        if socket.send(Message::Text(json)).await.is_err() {
            // Client disconnected
            break;
        }

        // 20Hz updates (50ms)
        sleep(Duration::from_millis(50)).await;
    }
}
