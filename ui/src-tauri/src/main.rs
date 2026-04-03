use serde::Serialize;
use std::env;
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
        .invoke_handler(tauri::generate_handler![launch_context])
        .run(tauri::generate_context!())
        .expect("failed to run Flow UI");
}
