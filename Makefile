# Makefile for QGIS Image Quality Toolset Plugin

.PHONY: build clean install test help

# Plugin name and version
PLUGIN_NAME = image_quality_toolset
VERSION = $(shell grep "^version=" $(PLUGIN_NAME)/metadata.txt | cut -d'=' -f2)

# Directories
BUILD_DIR = build
DIST_DIR = dist
SRC_DIR = $(PLUGIN_NAME)

# Shipped in the plugin folder: LICENSE (GPL v3, license_file of metadata.txt, required by
# plugins.qgis.org) and LICENSE-APACHE (Apache 2.0, the core algorithms)
LICENSE_FILES = LICENSE LICENSE-APACHE

# Files to exclude from the distribution
EXCLUDE_PATTERNS = \
	--exclude="*.pyc" \
	--exclude="__pycache__" \
	--exclude=".git" \
	--exclude=".gitignore" \
	--exclude=".pytest_cache" \
	--exclude=".ruff_cache" \
	--exclude=".vscode" \
	--exclude=".idea" \
	--exclude="*.py~" \
	--exclude="*~" \
	--exclude=".DS_Store" \
	--exclude="Thumbs.db" \
	--exclude="*.zip" \
	--exclude="dist/" \
	--exclude="build/"

help:
	@echo "Available targets:"
	@echo "  build   - Build the plugin distribution package"
	@echo "  clean   - Clean build artifacts"
	@echo "  install - Install the plugin to QGIS (requires QGIS installation)"
	@echo "  test    - Run tests using tox (recommended)"

build: clean
	@echo "Building plugin version $(VERSION)..."
	@mkdir -p $(DIST_DIR)
	@mkdir -p $(BUILD_DIR)/$(PLUGIN_NAME)
	@echo "Copying plugin files..."
	@rsync -av $(EXCLUDE_PATTERNS) $(SRC_DIR)/ $(BUILD_DIR)/$(PLUGIN_NAME)/
	@echo "Copying license files (dual license: GPL v3 plugin layer, Apache 2.0 core algorithms)..."
	@cp $(LICENSE_FILES) $(BUILD_DIR)/$(PLUGIN_NAME)/
	@echo "Creating distribution package..."
	@cd $(BUILD_DIR) && zip -r ../$(DIST_DIR)/$(PLUGIN_NAME).$(VERSION).zip $(PLUGIN_NAME)/
	@echo "Plugin package created: $(DIST_DIR)/$(PLUGIN_NAME).$(VERSION).zip"
	@ls -lh $(DIST_DIR)/$(PLUGIN_NAME).$(VERSION).zip

clean:
	@echo "Cleaning build artifacts..."
	@if [ -d "$(BUILD_DIR)" ]; then rm -rf $(BUILD_DIR); fi
	@if [ -d "$(DIST_DIR)" ]; then rm -rf $(DIST_DIR); fi
	@echo "Build cleaned."

install: build
	@echo "Installing plugin to QGIS..."
	@mkdir -p ~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/$(PLUGIN_NAME)
	@unzip -o $(DIST_DIR)/$(PLUGIN_NAME).$(VERSION).zip -d ~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/
	@echo "Plugin installed to QGIS."

test:
	@echo "Running tests with tox (this is the recommended way)..."
	@tox
	@echo "Tests completed."

test-local:
	@echo "Running tests locally (requires dependencies installed)..."
	@python run_tests.py
	@echo "Local tests completed."