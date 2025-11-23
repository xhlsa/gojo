use anyhow::Result;
use rerun::{archetypes::Scalar, RecordingStreamBuilder};

fn main() -> Result<()> {
    let output_path = "test_rerun_output.rrd";
    let rec = RecordingStreamBuilder::new("test_app")
        .save(output_path)
        .map_err(|e| anyhow::anyhow!("Failed: {}", e))?;
    
    eprintln!("[TEST] Rerun initialized successfully!");
    
    rec.set_time_seconds("stable_time", 0.0);
    let scalar = Scalar::new(42.0);
    let _ = rec.log("test/scalar", &scalar);
    
    eprintln!("[TEST] Logged scalar value");
    
    Ok(())
}
