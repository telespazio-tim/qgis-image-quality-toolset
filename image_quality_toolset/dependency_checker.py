# -*- coding: utf-8 -*-
"""
Dependency Checker Module - Generic Python dependency management for QGIS plugins

This module provides utilities to check and install Python dependencies with:
- Automatic version checking using importlib.metadata
- Isolated installation to plugin directory
- Background execution with progress reporting
- Cross-platform support (Windows, Linux, macOS)

Copyright (C) 2026 Telespazio
License: GPL-3.0
"""

import importlib
import importlib.util
import logging
import hashlib
import os
import platform
import re
import sys
import tempfile
import urllib.request
import zipfile
from typing import Dict, List, Optional, Tuple, Callable, Any


# Prebuilt wheel packs (loess, scikit-image and friends) attached to the GitHub release of each version,
# one per platform and Python: deps-<platform>-cpXY.zip. Their sha256 ships inside the plugin zip
# (deps-sha256.txt), so a pack is only unpacked if it is the one this plugin version was released with.
PACK_BASE_URL = "https://github.com/telespazio-tim/qgis-image-quality-toolset/releases/download/v{version}"


def platform_id() -> str:
    """Platform part of the wheel pack name."""
    if sys.platform.startswith("win"):
        return "win-x64"
    if sys.platform == "darwin":
        return "macos-arm64" if platform.machine().lower() in ("arm64", "aarch64") else "macos-x64"
    return "linux-x64"  # ponytail: no linux-arm64 bundle yet; add it to the packs job when needed


def pack_name() -> str:
    return f"deps-{platform_id()}-cp{sys.version_info.major}{sys.version_info.minor}.zip"


def install_wheel_pack(plugin_dir: str, progress_callback: Optional[Callable[[str], None]] = None) -> bool:
    """
    Download (or take from the plugin folder, for offline installs) the dependency bundle matching this
    platform and Python, check its sha256 and unpack it into dependencies_dir().

    :returns: True if a bundle was unpacked
    """
    say = progress_callback or (lambda m: None)
    name = pack_name()
    hashes_file = os.path.join(plugin_dir, "deps-sha256.txt")
    if not os.path.exists(hashes_file):
        logger.info("No deps-sha256.txt: running from source")
        say("✗ This copy of the plugin has no dependency bundle (running from source?). "
            "Install loess and scikit-image in the Python of QGIS, or use the released plugin zip.")
        return False
    with open(hashes_file, encoding="utf-8") as f:
        hashes = {line.split()[1]: line.split()[0] for line in f if len(line.split()) == 2}
    if name not in hashes:
        say(f"✗ No dependency bundle is published for {platform_id()} with Python "
            f"{sys.version_info.major}.{sys.version_info.minor} ({name}).")
        return False

    with open(os.path.join(plugin_dir, "metadata.txt"), encoding="utf-8") as f:
        version = next(l.split("=", 1)[1].strip() for l in f if l.startswith("version="))
    url = os.environ.get("IQT_PACK_BASE_URL", PACK_BASE_URL.format(version=version)) + "/" + name

    local = os.path.join(plugin_dir, name)  # offline install: the user drops the bundle here
    downloaded = None
    try:
        if os.path.exists(local):
            say(f"Using the dependency bundle found in the plugin folder: {name}")
        else:
            say(f"Downloading dependency bundle {name}...")
            fd, downloaded = tempfile.mkstemp(suffix=".zip", dir=plugin_dir)
            with os.fdopen(fd, "wb") as out, urllib.request.urlopen(url, timeout=60) as r:
                out.write(r.read())
            local = downloaded
        with open(local, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        if digest != hashes[name]:
            raise ValueError("checksum mismatch: not the bundle this plugin version was released with")
        say(f"Installing dependency bundle {name}...")
        with zipfile.ZipFile(local) as z:
            z.extractall(dependencies_dir(plugin_dir))
        importlib.invalidate_caches()
        return True
    except Exception as e:
        logger.error(f"Dependency bundle {name} not installed: {e}")
        say(f"✗ Could not get the dependency bundle {name}: {e}")
        say(f"To install it by hand: download {url}, put the file in {plugin_dir} (do not unzip it), "
            "and restart QGIS.")
        return False
    finally:
        if downloaded and os.path.exists(downloaded):
            os.remove(downloaded)


def dependencies_dir(plugin_dir: str) -> str:
    """Folder of the bundled/installed dependencies for the running Python (compiled wheels are per Python)."""
    return os.path.join(plugin_dir, "dependencies", f"cp{sys.version_info.major}{sys.version_info.minor}")

try:
    from importlib import metadata
except ImportError:
    metadata = None

# Configure logging
logger = logging.getLogger("qgis_plugin_deps")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)


class DependencyInfo:
    """Information about a single dependency."""

    def __init__(
        self,
        name: str,
        import_name: str,
        package: Optional[str] = None,
        min_version: Optional[str] = None,
        optional: bool = False,
    ):
        """
        Initialize dependency info.

        :param name: Human-readable name (e.g., "OpenCV")
        :param import_name: Python import name (e.g., "cv2")
        :param package: Pip package name (defaults to import_name)
        :param min_version: Minimum version string (e.g., "4.0.0")
        :param optional: Whether the dependency is optional
        """
        self.name = name
        self.import_name = import_name
        self.package = package or import_name
        self.min_version = min_version
        self.optional = optional


class DependencyStatus:
    """Status of a single dependency check."""

    def __init__(
        self,
        info: DependencyInfo,
        installed: bool = False,
        version: Optional[str] = None,
        error: Optional[str] = None,
        version_ok: bool = True,
        path: Optional[str] = None,
    ):
        self.info = info
        self.installed = installed
        self.version = version
        self.error = error
        self.version_ok = version_ok
        self.path = path

    def __str__(self) -> str:
        if self.installed:
            version_str = f" (v{self.version})" if self.version else ""
            status = "✓" if self.version_ok else "⚠"
            if not self.version_ok:
                return f"{status} {self.info.name}{version_str} - Needs v{self.info.min_version}"
            return f"{status} {self.info.name}{version_str}"
        else:
            status = "✗" if not self.info.optional else "○"
            return f"{status} {self.info.name} (missing)"


class DependencyChecker:
    """
    Check and install Python dependencies with cross-platform support.
    """

    def __init__(self, dependencies: List[DependencyInfo]):
        """
        Initialize the dependency checker.

        :param dependencies: List of DependencyInfo objects
        """
        self.dependencies = dependencies
        self.results: Dict[str, DependencyStatus] = {}

    def _parse_version(self, version_str: str) -> Tuple[int, ...]:
        """Parse version string to comparable tuple."""
        if not version_str:
            return (0,)
        match = re.match(r"([\d.]+)", str(version_str))
        if match:
            parts = match.group(1).split(".")
            return tuple(int(p) for p in parts if p.isdigit())
        return (0,)

    def _check_version(self, actual: str, minimum: str) -> bool:
        """Check if actual version meets minimum requirement."""
        a = self._parse_version(actual)
        m = self._parse_version(minimum)
        # Pad with zeros for correct comparison (e.g., 1.7 >= 1.7.0)
        max_len = max(len(a), len(m))
        a_padded = a + (0,) * (max_len - len(a))
        m_padded = m + (0,) * (max_len - len(m))
        return a_padded >= m_padded

    def _get_module_version(self, info: DependencyInfo) -> Optional[str]:
        """Try to get the version of an installed module without full import if possible."""
        # 1. Try metadata (package name)
        if metadata:
            try:
                return metadata.version(info.package)
            except Exception:
                pass

            # 2. Try metadata (import name)
            try:
                return metadata.version(info.import_name)
            except Exception:
                pass

        # 3. Fallback: try to import and look for version attributes
        try:
            # Check if already in sys.modules to avoid side effects of re-import
            if info.import_name in sys.modules:
                module = sys.modules[info.import_name]
            else:
                # Use find_spec to see if it's available before importing
                spec = importlib.util.find_spec(info.import_name)
                if spec is None:
                    return None
                module = importlib.import_module(info.import_name)

            for attr in ["__version__", "version", "VERSION"]:
                if hasattr(module, attr):
                    version = getattr(module, attr)
                    if isinstance(version, tuple):
                        return ".".join(str(v) for v in version)
                    return str(version)
        except Exception:
            pass

        return None

    def check_single(self, info: DependencyInfo) -> DependencyStatus:
        """Check a single dependency."""
        try:
            # First check if it can be found without importing
            spec = importlib.util.find_spec(info.import_name)
            if spec is None:
                return DependencyStatus(info=info, installed=False)

            version = self._get_module_version(info)

            # Determine path
            path = getattr(spec, "origin", None)
            if path is None and hasattr(spec, "submodule_search_locations"):
                locs = spec.submodule_search_locations
                if locs:
                    path = list(locs)[0]

            version_ok = True
            if info.min_version and version:
                version_ok = self._check_version(version, info.min_version)

            return DependencyStatus(
                info=info,
                installed=True,
                version=version,
                path=path,
                version_ok=version_ok,
            )
        except Exception as e:
            logger.debug(f"Error checking {info.name}: {e}")
            return DependencyStatus(info=info, installed=False, error=str(e))

    def check_all(self) -> Dict[str, DependencyStatus]:
        """Check all dependencies."""
        self.results = {}
        for info in self.dependencies:
            self.results[info.import_name] = self.check_single(info)
        return self.results

    def has_missing(self) -> bool:
        """Check if there are any missing or invalid non-optional dependencies."""
        if not self.results:
            self.check_all()
        return any(
            (not s.installed or not s.version_ok) and not s.info.optional
            for s in self.results.values()
        )

    def get_missing(self) -> List[DependencyStatus]:
        """Get list of missing or invalid non-optional dependencies."""
        if not self.results:
            self.check_all()
        return [
            s
            for s in self.results.values()
            if (not s.installed or not s.version_ok) and not s.info.optional
        ]

    def summary(self) -> str:
        """Get a human-readable summary of dependency status."""
        if not self.results:
            self.check_all()

        lines = ["Dependency Status:", "-" * 40]
        for status in self.results.values():
            lines.append(str(status))

        total = len(self.results)
        installed = sum(
            1 for s in self.results.values() if s.installed and s.version_ok
        )

        lines.append("-" * 40)
        lines.append(f"Satisfied: {installed}/{total}")
        return "\n".join(lines)


# ============================================================================
# Background Dependency Checking (Qt Thread)
# ============================================================================

try:
    from qgis.PyQt.QtCore import QObject, pyqtSignal, QThread
    from typing import Optional as QOptional

    HAS_QT = True
except ImportError:
    HAS_QT = False


if HAS_QT:

    class DependencyWorkerSignals(QObject):
        """Signals for the dependency worker thread."""

        started = pyqtSignal()
        finished = pyqtSignal(bool)
        progress_message = pyqtSignal(str)
        log_message = pyqtSignal(str, int)

    class DependencyWorker(QThread):
        """
        Background worker for checking and installing dependencies.
        Runs in a separate thread to avoid blocking the UI.
        """

        def __init__(
            self,
            plugin_dir: str,
            dependencies: list,
            parent: QOptional[QObject] = None,
        ):
            super().__init__(parent)
            self.plugin_dir = plugin_dir
            self.dependencies = dependencies
            self.signals = DependencyWorkerSignals()
            self._result = False
            self.messages = []

        def run(self):
            """Execute dependency check in background thread."""
            try:
                logger.info("DependencyWorker.run() started")
                self.signals.started.emit()
                self.signals.progress_message.emit("Checking dependencies...")

                # Add dependencies folder to sys.path
                deps_dir = dependencies_dir(self.plugin_dir)
                os.makedirs(deps_dir, exist_ok=True)

                if deps_dir not in sys.path:
                    sys.path.insert(0, deps_dir)
                    logger.info(f"Added dependencies to path: {deps_dir}")

                # Progress callback
                def on_progress(message: str):
                    self.messages.append(message)
                    self.signals.progress_message.emit(message)

                # Run check and install
                logger.info("Calling check_and_install_dependencies()...")
                self._result = check_and_install_dependencies(
                    self.dependencies, self.plugin_dir, progress_callback=on_progress
                )
                logger.info(f"check_and_install_dependencies() returned: {self._result}")

            except Exception as e:
                logger.error(f"Dependency worker error: {e}", exc_info=True)
                err_msg = f"Critical Error: {e}"
                self.messages.append(err_msg)
                self.signals.log_message.emit(err_msg, 2)
                self._result = False
            finally:
                self.signals.finished.emit(self._result)

    class BackgroundDependencyChecker:
        """
        Manages background dependency checking with progress reporting.
        """

        def __init__(
            self,
            iface: Any,
            plugin_dir: str,
            dependencies: list,
            on_complete: QOptional[Callable[[bool], None]] = None,
            result_dockwidget: Any = None,
        ):
            self.iface = iface
            self.plugin_dir = plugin_dir
            self.dependencies = dependencies
            self.on_complete = on_complete
            self.result_dockwidget = result_dockwidget
            self.worker: QOptional[DependencyWorker] = None

        def start(self):
            """Start background dependency checking."""
            print("=" * 80)
            print("BackgroundDependencyChecker: start() called")
            print("=" * 80)

            def update_progress(message: str, is_error: bool = False):
                if not message:
                    return
                if self.result_dockwidget and hasattr(
                    self.result_dockwidget, "set_progress_visible"
                ):
                    self.result_dockwidget.set_progress_visible(True)
                    self.result_dockwidget.set_progress_status(message, is_error)

            self.worker = DependencyWorker(self.plugin_dir, self.dependencies)
            self.worker.signals.started.connect(
                lambda: update_progress("Checking dependencies...")
            )
            self.worker.signals.progress_message.connect(
                lambda msg: update_progress(self._extract_status_message(msg))
            )
            self.worker.signals.finished.connect(self._on_finished)
            self.worker.start()

        def _extract_status_message(self, msg: str) -> str:
            """Status line shown in the panel: messages are written to be shown as they are."""
            if msg == "All dependencies satisfied":
                return "✓ Dependencies ready"
            return msg

        def _on_finished(self, success: bool):
            """Handle worker completion."""
            if self.result_dockwidget and hasattr(
                self.result_dockwidget, "set_progress_visible"
            ):
                if success:
                    self.result_dockwidget.set_progress_status("✓ Dependencies ready")
                else:
                    # Installation failed
                    if hasattr(self.worker, "messages"):
                        # Don't overwrite, merge if possible
                        if hasattr(self.result_dockwidget, "_all_messages"):
                            for m in self.worker.messages:
                                if m not in self.result_dockwidget._all_messages:
                                    self.result_dockwidget._all_messages.append(m)

                    self.result_dockwidget.set_progress_status(
                        "⚠ Dependencies missing - click for details", is_error=True
                    )

            if self.on_complete:
                self.on_complete(success)

            if self.worker:
                self.worker.deleteLater()
                self.worker = None

    def check_dependencies_background(
        iface: Any,
        plugin_dir: str,
        dependencies: list,
        on_complete: QOptional[Callable[[bool], None]] = None,
        result_dockwidget: Any = None,
    ) -> BackgroundDependencyChecker:
        """
        Start background dependency checking.
        """
        print("=" * 80)
        print("check_dependencies_background() called")
        print("=" * 80)
        checker = BackgroundDependencyChecker(
            iface, plugin_dir, dependencies, on_complete, result_dockwidget
        )
        checker.start()
        return checker


# ============================================================================
# Main Installation Function
# ============================================================================


def check_and_install_dependencies(
    dependencies: List[DependencyInfo],
    plugin_dir: str,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> bool:
    """
    Check dependencies and unpack the dependency bundle into the plugin directory if some are missing.

    :param dependencies: List of DependencyInfo objects
    :param plugin_dir: Plugin directory for isolated installation
    :param progress_callback: Optional callback(message: str) for real-time progress
    :returns: True if all dependencies are satisfied
    """
    logger.info("=" * 60)
    logger.info("check_and_install_dependencies() CALLED")
    logger.info("=" * 60)
    logger.info("Starting dependency check")

    target_dir = dependencies_dir(plugin_dir)
    os.makedirs(target_dir, exist_ok=True)

    if target_dir not in sys.path:
        sys.path.insert(0, target_dir)

    checker = DependencyChecker(dependencies)
    checker.check_all()

    missing = checker.get_missing()
    if not missing:
        logger.info("All dependencies satisfied")
        return True

    logger.info(f"Installing {len(missing)} missing or outdated dependencies")
    if progress_callback:
        progress_callback("Missing: " + ", ".join(st.info.name for st in missing) + " - getting the dependency bundle")

    # numpy/scipy/matplotlib come with QGIS; the rest is in the dependency bundle
    if install_wheel_pack(plugin_dir, progress_callback):
        checker.check_all()
    if checker.has_missing():
        names = ", ".join(st.info.name for st in checker.get_missing())
        logger.error(f"Missing dependencies: {names}")
        if progress_callback:
            progress_callback(f"✗ Still missing: {names}")
            if any(st.info.import_name in ("numpy", "scipy", "matplotlib") for st in checker.get_missing()):
                progress_callback("NumPy, SciPy and Matplotlib are part of QGIS: repair or reinstall QGIS.")
        return False

    logger.info("All dependencies installed and verified")
    if progress_callback:
        progress_callback("All dependencies satisfied")
    return True


# ============================================================================
# Example Usage
# ============================================================================

if __name__ == "__main__":
    # Example configuration
    EXAMPLE_DEPENDENCIES = [
        DependencyInfo("NumPy", "numpy", min_version="1.20.0"),
        DependencyInfo("SciPy", "scipy", min_version="1.7.0"),
        DependencyInfo("Matplotlib", "matplotlib", min_version="3.4.0"),
        DependencyInfo("loess", "loess", min_version="2.1.2"),
        DependencyInfo("scikit-image", "skimage", package="scikit-image"),
    ]

    checker = DependencyChecker(EXAMPLE_DEPENDENCIES)
    print(checker.summary())

    if checker.has_missing():
        print("\nSome dependencies are missing or outdated.")
    else:
        print("\n✓ All dependencies satisfied!")
