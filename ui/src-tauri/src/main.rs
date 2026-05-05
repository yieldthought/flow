use serde::Serialize;
use std::env;
use std::process::Command;
use std::sync::Mutex;

#[derive(Clone, Serialize)]
struct LaunchContext {
    #[serde(rename = "flowName")]
    flow_name: String,
    #[serde(rename = "apiBaseUrl")]
    api_base_url: String,
}

struct SharedLaunchContext(Mutex<LaunchContext>);

#[tauri::command]
fn launch_context(state: tauri::State<'_, SharedLaunchContext>) -> LaunchContext {
    state.0.lock().expect("launch context lock poisoned").clone()
}

#[tauri::command]
fn open_external_url(url: String) -> Result<(), String> {
    let trimmed = url.trim();
    if !(trimmed.starts_with("http://") || trimmed.starts_with("https://")) {
        return Err("only http and https links can be opened".to_string());
    }
    open_with_system(trimmed)
}

#[cfg(target_os = "macos")]
fn open_with_system(url: &str) -> Result<(), String> {
    Command::new("open")
        .arg(url)
        .spawn()
        .map(|_| ())
        .map_err(|error| error.to_string())
}

#[cfg(target_os = "windows")]
fn open_with_system(url: &str) -> Result<(), String> {
    Command::new("cmd")
        .args(["/C", "start", "", url])
        .spawn()
        .map(|_| ())
        .map_err(|error| error.to_string())
}

#[cfg(all(not(target_os = "macos"), not(target_os = "windows")))]
fn open_with_system(url: &str) -> Result<(), String> {
    Command::new("xdg-open")
        .arg(url)
        .spawn()
        .map(|_| ())
        .map_err(|error| error.to_string())
}

fn parse_launch_context() -> LaunchContext {
    let mut flow_name = env::var("FLOW_UI_FLOW_NAME").unwrap_or_default();
    let mut api_base_url = env::var("FLOW_UI_API_BASE_URL").unwrap_or_default();
    let mut args = env::args().skip(1);

    while let Some(item) = args.next() {
        match item.as_str() {
            "--flow-name" => {
                if let Some(value) = args.next() {
                    flow_name = value;
                }
            }
            "--api-base-url" => {
                if let Some(value) = args.next() {
                    api_base_url = value;
                }
            }
            _ => {}
        }
    }

    LaunchContext {
        flow_name,
        api_base_url,
    }
}

fn main() {
    tauri::Builder::default()
        .manage(SharedLaunchContext(Mutex::new(parse_launch_context())))
        .invoke_handler(tauri::generate_handler![launch_context, open_external_url])
        .run(tauri::generate_context!())
        .expect("failed to run Flow UI");
}
