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
import os
import re
import subprocess
import sys
from typing import Dict, List, Optional, Tuple, Callable, Any

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

    def _get_pip_command(self) -> List[str]:
        """Get the appropriate pip command for the current platform."""
        python_executable = sys.executable
        # Handle QGIS on Windows where sys.executable might be qgis-bin.exe
        if os.path.basename(python_executable).lower().endswith(".exe"):
            python_w = os.path.join(os.path.dirname(python_executable), "pythonw.exe")
            python_exe = os.path.join(os.path.dirname(python_executable), "python.exe")
            if os.path.exists(python_exe):
                python_executable = python_exe
            elif os.path.exists(python_w):
                python_executable = python_w

        # On macOS/Linux bundles sys.executable is the QGIS binary itself:
        # running "QGIS -m pip ..." would start a new QGIS (which reloads the
        # plugin and spawns another one, endlessly). Find a real Python.
        if not os.path.basename(python_executable).lower().startswith("python"):
            exe_dir = os.path.dirname(sys.executable)
            candidates = [
                os.path.join(sys.exec_prefix, "bin", "python3"),
                os.path.join(sys.prefix, "bin", "python3"),
                os.path.join(exe_dir, "bin", "python3"),
                os.path.join(exe_dir, "python3"),
            ]
            python_executable = next((c for c in candidates if os.path.exists(c)), None)
            if python_executable is None:
                raise RuntimeError("No Python interpreter found to run pip")

        return [python_executable, "-m", "pip"]

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

    def install_missing(
        self,
        target_dir: Optional[str] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Tuple[bool, str]]:
        """
        Install all missing non-optional dependencies.

        :param target_dir: Directory to install packages (isolated)
        :param progress_callback: Optional callback for progress messages
        :returns: Dictionary mapping import names to (success, message) tuples
        """
        if not self.results:
            self.check_all()

        results = {}
        missing = self.get_missing()

        for status in missing:
            success, msg = self._install_single(
                status.info, target_dir=target_dir, callback=progress_callback
            )
            results[status.info.import_name] = (success, msg)

            # Re-check this one immediately to update internal state
            self.results[status.info.import_name] = self.check_single(status.info)

        return results

    def _install_single(
        self,
        info: DependencyInfo,
        target_dir: Optional[str] = None,
        callback: Optional[Callable[[str], None]] = None,
    ) -> Tuple[bool, str]:
        """Install a single dependency."""
        try:
            pip_cmd = self._get_pip_command()
        except RuntimeError as e:
            msg = f"✗ Cannot install {info.name}: {e}"
            if callback:
                callback(msg)
            return False, msg
        package_spec = info.package

        # If it was installed but version was too low, we need to upgrade
        if info.min_version:
            package_spec = f"{info.package}>={info.min_version}"

        cmd = pip_cmd + ["install", package_spec]

        if target_dir:
            cmd += ["--target", target_dir, "--upgrade", "--no-cache-dir"]

        if callback:
            callback(f"Installing {info.name}...")

        try:
            logger.info(f"Running command: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

            if result.returncode == 0:
                # We don't re-check here, install_missing will do it or caller will
                msg = f"✓ {info.name} installed successfully"
                if callback:
                    callback(msg)
                return True, msg
            else:
                error_msg = result.stderr.strip() or result.stdout.strip()
                # Clean up error message if it's too long
                if len(error_msg) > 200:
                    error_msg = error_msg[:197] + "..."
                msg = f"✗ Failed to install {info.name}: {error_msg}"
                if callback:
                    callback(msg)
                return False, msg

        except subprocess.TimeoutExpired:
            msg = f"✗ Installation of {info.name} timed out"
            if callback:
                callback(msg)
            return False, msg
        except Exception as e:
            msg = f"✗ Error installing {info.name}: {str(e)}"
            if callback:
                callback(msg)
            return False, msg

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
                dependencies_dir = os.path.join(self.plugin_dir, "dependencies")
                if not os.path.exists(dependencies_dir):
                    os.makedirs(dependencies_dir, exist_ok=True)

                if dependencies_dir not in sys.path:
                    sys.path.insert(0, dependencies_dir)
                    logger.info(f"Added dependencies to path: {dependencies_dir}")

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
            """Extract a clean status message for progress display."""
            if not msg:
                return ""

            if "Installing" in msg:
                match = re.search(r"Installing\s+([^\s(]+)", msg)
                if match:
                    return f"Installing {match.group(1)}..."
                return "Installing dependencies..."

            if "successfully" in msg or "✓" in msg:
                match = re.search(r"[✓]\s+([^\s]+)\s+installed", msg)
                if match:
                    return f"✓ {match.group(1)} ready"
                return "✓ Dependency ready"

            if "All dependencies satisfied" in msg:
                return "✓ Dependencies ready"

            # If it's an error, return it as is (will be handled by update_progress)
            if "✗" in msg or "Failed" in msg or "Error" in msg:
                return msg

            return msg if len(msg) < 60 else None

        def _on_finished(self, success: bool):
            """Handle worker completion."""
            if self.result_dockwidget and hasattr(
                self.result_dockwidget, "set_progress_visible"
            ):
                if success:
                    self.result_dockwidget.set_progress_visible(False)
                else:
                    # Installation failed
                    if hasattr(self.worker, "messages"):
                        # Don't overwrite, merge if possible
                        if hasattr(self.result_dockwidget, "_all_messages"):
                            for m in self.worker.messages:
                                if m not in self.result_dockwidget._all_messages:
                                    self.result_dockwidget._all_messages.append(m)

                    self.result_dockwidget.set_progress_status(
                        "⚠ Dependency error - click for details", is_error=True
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
    Check dependencies and install missing ones to plugin directory.

    :param dependencies: List of DependencyInfo objects
    :param plugin_dir: Plugin directory for isolated installation
    :param progress_callback: Optional callback(message: str) for real-time progress
    :returns: True if all dependencies are satisfied
    """
    logger.info("=" * 60)
    logger.info("check_and_install_dependencies() CALLED")
    logger.info("=" * 60)
    logger.info("Starting dependency check")

    target_dir = os.path.join(plugin_dir, "dependencies")
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
        progress_callback(f"Found {len(missing)} missing packages...")

    # Use the checker to install missing ones
    checker.install_missing(target_dir=target_dir, progress_callback=progress_callback)

    # Final re-check
    checker.check_all()
    if checker.has_missing():
        remaining = checker.get_missing()
        for s in remaining:
            logger.error(f"Failed to satisfy dependency: {s.info.name}")
        return False

    logger.info("All dependencies installed and verified")
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
