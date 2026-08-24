.PHONY: install-system install-user uninstall-system uninstall-user installer-help deb clean-deb help

PYTHON ?= python3
START ?= 1
SETCAP ?= 0

# Debian package settings
PACKAGE_NAME = dmx-lan-bridge
VERSION = 2.0.0
DEB_BUILD_DIR = packaging/debian
DEB_PKG_DIR = $(DEB_BUILD_DIR)/$(PACKAGE_NAME)
DEB_OUTPUT_DIR = dist

help:
	@echo "DMX LAN Bridge - Makefile targets:"
	@echo ""
	@echo "  install-system   Install system service (requires root)"
	@echo "  install-user     Install user service"
	@echo "  uninstall-system Uninstall system service (requires root)"
	@echo "  uninstall-user   Uninstall user service"
	@echo "  deb              Build Debian package"
	@echo "  clean-deb        Remove Debian build artifacts"
	@echo "  installer-help   Show installer script help"
	@echo ""

installer-help:
	@./scripts/install.sh --help

install-system:
	./scripts/install.sh install --system $(if $(filter 0,$(START)),--no-start,) $(if $(filter 1,$(SETCAP)),--setcap,) --python $(PYTHON)

install-user:
	./scripts/install.sh install --user $(if $(filter 0,$(START)),--no-start,) $(if $(filter 1,$(SETCAP)),--setcap,) --python $(PYTHON)

uninstall-system:
	./scripts/install.sh uninstall --system --python $(PYTHON)

uninstall-user:
	./scripts/install.sh uninstall --user --python $(PYTHON)

# Build Debian package
deb: clean-deb
	@echo "Building Debian package $(PACKAGE_NAME) $(VERSION)..."
	@mkdir -p $(DEB_PKG_DIR)/DEBIAN
	@mkdir -p $(DEB_PKG_DIR)/usr/bin
	@mkdir -p $(DEB_PKG_DIR)/lib/systemd/system
	@mkdir -p $(DEB_PKG_DIR)/etc/dmx-bridge
	@mkdir -p $(DEB_PKG_DIR)/usr/share/doc/$(PACKAGE_NAME)

	@# Create control file from template
	@sed "s/\$${VERSION}/$(VERSION)/" packaging/debian/control.template > $(DEB_PKG_DIR)/DEBIAN/control

	@# Copy package scripts
	@cp packaging/debian/preinst $(DEB_PKG_DIR)/DEBIAN/preinst
	@cp packaging/debian/postinst $(DEB_PKG_DIR)/DEBIAN/postinst
	@cp packaging/debian/prerm $(DEB_PKG_DIR)/DEBIAN/prerm
	@cp packaging/debian/postrm $(DEB_PKG_DIR)/DEBIAN/postrm
	@cp packaging/debian/conffiles $(DEB_PKG_DIR)/DEBIAN/conffiles
	@chmod 755 $(DEB_PKG_DIR)/DEBIAN/preinst $(DEB_PKG_DIR)/DEBIAN/postinst $(DEB_PKG_DIR)/DEBIAN/prerm $(DEB_PKG_DIR)/DEBIAN/postrm

	@# Install Python source files to /opt/dmx-lan-bridge
	@mkdir -p $(DEB_PKG_DIR)/opt/dmx-lan-bridge
	@cp -r src/dmx_lan_bridge $(DEB_PKG_DIR)/opt/dmx-lan-bridge/

	@# Install capability catalogs to standard location
	@mkdir -p $(DEB_PKG_DIR)/usr/share/dmx_lan_bridge
	@cp res/capability_catalog_govee.json $(DEB_PKG_DIR)/usr/share/dmx_lan_bridge/
	@cp res/capability_catalog_lifx.json $(DEB_PKG_DIR)/usr/share/dmx_lan_bridge/

	@# Create executable wrappers using system Python
	@echo '#!/bin/bash' > $(DEB_PKG_DIR)/usr/bin/dmx-lan-bridge
	@echo 'export PYTHONPATH="/opt/dmx-lan-bridge:$$PYTHONPATH"' >> $(DEB_PKG_DIR)/usr/bin/dmx-lan-bridge
	@echo 'exec python3 -c "from dmx_lan_bridge.__main__ import run; import sys; sys.exit(run())" "$$@"' >> $(DEB_PKG_DIR)/usr/bin/dmx-lan-bridge
	@chmod +x $(DEB_PKG_DIR)/usr/bin/dmx-lan-bridge

	@echo '#!/bin/bash' > $(DEB_PKG_DIR)/usr/bin/dmx-lan-cli
	@echo 'export PYTHONPATH="/opt/dmx-lan-bridge:$$PYTHONPATH"' >> $(DEB_PKG_DIR)/usr/bin/dmx-lan-cli
	@echo 'exec python3 -c "from dmx_lan_bridge.cli import main; import sys; sys.exit(main())" "$$@"' >> $(DEB_PKG_DIR)/usr/bin/dmx-lan-cli
	@chmod +x $(DEB_PKG_DIR)/usr/bin/dmx-lan-cli

	@# Install systemd service
	@cp packaging/systemd/dmx-lan-bridge.service $(DEB_PKG_DIR)/lib/systemd/system/

	@# Install config template as conffile
	@cp packaging/config/dmx-bridge.toml $(DEB_PKG_DIR)/etc/dmx-bridge/config.toml

	@# Install documentation
	@cp README.md $(DEB_PKG_DIR)/usr/share/doc/$(PACKAGE_NAME)/
	@cp LICENSE $(DEB_PKG_DIR)/usr/share/doc/$(PACKAGE_NAME)/
	@cp INSTALL.md $(DEB_PKG_DIR)/usr/share/doc/$(PACKAGE_NAME)/
	@cp packaging/config/dmx-bridge.toml.example $(DEB_PKG_DIR)/usr/share/doc/$(PACKAGE_NAME)/
	@gzip -9 -n -c INSTALL.md > $(DEB_PKG_DIR)/usr/share/doc/$(PACKAGE_NAME)/INSTALL.md.gz

	@# Create copyright file
	@echo "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/" > $(DEB_PKG_DIR)/usr/share/doc/$(PACKAGE_NAME)/copyright
	@echo "Upstream-Name: $(PACKAGE_NAME)" >> $(DEB_PKG_DIR)/usr/share/doc/$(PACKAGE_NAME)/copyright
	@echo "Source: https://github.com/mccartyp/dmx-lan-bridge" >> $(DEB_PKG_DIR)/usr/share/doc/$(PACKAGE_NAME)/copyright
	@echo "" >> $(DEB_PKG_DIR)/usr/share/doc/$(PACKAGE_NAME)/copyright
	@echo "Files: *" >> $(DEB_PKG_DIR)/usr/share/doc/$(PACKAGE_NAME)/copyright
	@echo "Copyright: 2025 Patrick McCarty" >> $(DEB_PKG_DIR)/usr/share/doc/$(PACKAGE_NAME)/copyright
	@echo "License: MIT" >> $(DEB_PKG_DIR)/usr/share/doc/$(PACKAGE_NAME)/copyright
	@cat LICENSE >> $(DEB_PKG_DIR)/usr/share/doc/$(PACKAGE_NAME)/copyright

	@# Set permissions
	@find $(DEB_PKG_DIR) -type d -exec chmod 755 {} \;
	@find $(DEB_PKG_DIR) -type f -exec chmod 644 {} \;
	@chmod 755 $(DEB_PKG_DIR)/usr/bin/dmx-lan-bridge
	@chmod 755 $(DEB_PKG_DIR)/usr/bin/dmx-lan-cli
	@chmod 755 $(DEB_PKG_DIR)/DEBIAN/preinst
	@chmod 755 $(DEB_PKG_DIR)/DEBIAN/postinst
	@chmod 755 $(DEB_PKG_DIR)/DEBIAN/prerm
	@chmod 755 $(DEB_PKG_DIR)/DEBIAN/postrm
	@chmod 755 $(DEB_PKG_DIR)/DEBIAN

	@# Build package
	@mkdir -p $(DEB_OUTPUT_DIR)
	@dpkg-deb --build $(DEB_PKG_DIR) $(DEB_OUTPUT_DIR)
	@echo ""
	@echo "Debian package built successfully!"
	@ls -lh $(DEB_OUTPUT_DIR)/$(PACKAGE_NAME)_$(VERSION)_all.deb

# Clean build artifacts
clean-deb:
	@echo "Cleaning Debian build artifacts..."
	rm -rf $(DEB_PKG_DIR) $(DEB_OUTPUT_DIR)
	@echo "Clean complete!"
