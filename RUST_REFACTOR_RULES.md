# Rust Refactor Rules

1. No direct translation of Python logic; refactor designs to fit idiomatic Rust patterns.
2. Model state with enums, not free-form strings or booleans. Invalid states should be unrepresentable.
3. Minimize cloning. Prefer references over shared pointers (Arc/RefCell) wherever possible.
4. Propagate errors via `Result<T, E>`; never `unwrap()` or `panic!()` in production paths.
5. Replace Python-specific libraries with Rust equivalents (Serde for JSON, reqwest for HTTP, etc.).
