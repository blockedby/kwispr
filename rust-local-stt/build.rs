use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

fn main() {
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("linux") {
        return;
    }

    // Handy's Linux posture is a shared transcribe runtime plus dynamically
    // loaded CPU/Vulkan modules. Keep both beside the standalone server binary.
    println!("cargo:rustc-link-arg=-Wl,-rpath,$ORIGIN:$ORIGIN/..");
    stage_transcribe_runtime_libs();
}

fn stage_transcribe_runtime_libs() {
    println!("cargo:rerun-if-env-changed=DEP_TRANSCRIBE_CPP_RUNTIME_DIR");
    println!("cargo:rerun-if-env-changed=DEP_TRANSCRIBE_CPP_MODULE_DIR");

    let Some(runtime_dir) = std::env::var_os("DEP_TRANSCRIBE_CPP_RUNTIME_DIR") else {
        return;
    };
    let mut dirs = BTreeSet::from([PathBuf::from(runtime_dir)]);
    if let Some(module_dir) = std::env::var_os("DEP_TRANSCRIBE_CPP_MODULE_DIR") {
        dirs.insert(PathBuf::from(module_dir));
    }

    let out_dir = PathBuf::from(std::env::var_os("OUT_DIR").expect("OUT_DIR"));
    let destination = out_dir
        .ancestors()
        .nth(3)
        .expect("Cargo target profile directory")
        .to_path_buf();
    std::fs::create_dir_all(&destination).expect("create target profile directory");
    clean_staged_runtime_libs(&destination);

    let mut libraries: BTreeMap<String, PathBuf> = BTreeMap::new();
    for dir in dirs {
        for entry in std::fs::read_dir(&dir)
            .unwrap_or_else(|error| panic!("read {}: {error}", dir.display()))
            .flatten()
        {
            let source = entry.path();
            let name = source
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or("");
            if is_staged_runtime_name(name) {
                libraries.insert(name.to_owned(), source);
            }
        }
    }

    // Keep one real file for each library: SONAME for linked libraries and the
    // unversioned name for dlopen'd backend modules, mirroring Handy's staging.
    let mut best: BTreeMap<&str, (&str, &Path, usize)> = BTreeMap::new();
    for (name, source) in &libraries {
        let (stem, rank) = match split_versioned_so(name) {
            Some((stem, depth)) => (stem, if depth == 1 { 0 } else { depth + 1 }),
            None => (name.as_str(), 0),
        };
        if best
            .get(stem)
            .is_none_or(|(_, _, existing)| rank < *existing)
        {
            best.insert(stem, (name, source.as_path(), rank));
        }
    }

    let mut staged_names = Vec::new();
    for (_, (name, source, _)) in best {
        std::fs::copy(source, destination.join(name))
            .unwrap_or_else(|error| panic!("copy {}: {error}", source.display()));
        staged_names.push(name.to_owned());
    }
    assert!(
        staged_names
            .iter()
            .any(|name| name.starts_with("libtranscribe.so")),
        "transcribe-cpp shared runtime was not staged"
    );
    assert!(
        staged_names
            .iter()
            .any(|name| name.starts_with("libggml-cpu")),
        "transcribe-cpp CPU backend modules were not staged"
    );
}

fn clean_staged_runtime_libs(destination: &Path) {
    for entry in std::fs::read_dir(destination)
        .unwrap_or_else(|error| panic!("read {}: {error}", destination.display()))
        .flatten()
    {
        let path = entry.path();
        let name = path
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("");
        if path.is_file() && is_staged_runtime_name(name) {
            std::fs::remove_file(&path)
                .unwrap_or_else(|error| panic!("remove stale {}: {error}", path.display()));
        }
    }
}

fn is_staged_runtime_name(name: &str) -> bool {
    (name.starts_with("libtranscribe") || name.starts_with("libggml"))
        && split_versioned_so(name).is_some()
}

fn split_versioned_so(name: &str) -> Option<(&str, usize)> {
    let index = name.find(".so")?;
    let (stem, suffix) = (&name[..index], &name[index + 3..]);
    if suffix.is_empty() {
        return Some((stem, 0));
    }
    let components: Vec<&str> = suffix.strip_prefix('.')?.split('.').collect();
    components
        .iter()
        .all(|component| {
            !component.is_empty() && component.bytes().all(|byte| byte.is_ascii_digit())
        })
        .then_some((stem, components.len()))
}
