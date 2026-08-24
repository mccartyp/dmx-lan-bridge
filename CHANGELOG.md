# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - 2026-01-03

### Breaking Changes

#### Package & Command Renaming
- **Package renamed**: `govee-artnet-lan-bridge` → `dmx-lan-bridge` (reflects multi-protocol support)
- **Primary commands**: `govee-artnet-bridge` → `dmx-lan-bridge`, `govee-artnet-cli` → `dmx-lan-cli`
- **Configuration files**: `govee-bridge.toml` → `dmx-bridge.toml`
- **Systemd services**: `govee-bridge.service` and `govee-bridge-user.service` → `dmx-lan-bridge.service` (in the system and user scopes)
- **Capability catalogs**: Split into protocol-specific files (`capability_catalog_govee.json`, `capability_catalog_lifx.json`)

#### Configuration Changes
- Default universe changed from `0` to `1` for E1.31/sACN compatibility
- sACN/E1.31 input protocol now **enabled by default** (port 5568)
- Configuration file structure updated to support multiple input and device protocols
- ArtNet priority set to 25 (below sACN default of 100) for priority-based merging

#### API & Database Changes
- Device schema extended with `protocol` field (required for all devices)
- Device retrieval queries now filter by protocol
- Mapping events now include `channel`, `device_id`, `field`, and `fields` properties
- New device events: `device_enabled`, `device_disabled`

### Added

#### Multi-Protocol Input Support
- ✅ **sACN/E1.31 protocol support** (port 5568, enabled by default)
  - Native E1.31 priority support (0-200)
  - Multicast group management with dynamic membership
  - Universe discovery and automatic subscription
  - Full E1.31 specification compliance
- **Priority-based source merging** for multiple DMX controllers
  - Automatic failover when primary source stops (2.5s timeout per E1.31 spec)
  - Graceful priority transitions with logging
  - Support for simultaneous ArtNet and sACN sources
  - Universe-specific priority tracking
- **Unified DMX abstraction layer** (`dmx.py`)
  - Protocol-agnostic `DmxFrame` structure
  - `PriorityMerger` for HTP (Highest Takes Priority) merging
  - `DmxMappingService` for centralized mapping logic

#### Multi-Protocol Device Support
- ✅ **LIFX LAN protocol support** (UDP port 56700)
  - Binary protocol implementation with packet encoding/decoding
  - Device discovery via broadcast
  - Label discovery and version information
  - Firmware data collection in device extensions
  - Full capability catalog with 150+ LIFX products
  - Color, brightness, and color temperature control
- **Protocol abstraction layer** (`protocol/` module)
  - Base protocol handler interface
  - Protocol-specific implementations (Govee, LIFX)
  - Pluggable architecture for future protocols
  - Per-protocol discovery and command handling
- **Device polling service** with health monitoring
  - Degraded state detection for intermittent devices
  - Configurable poll intervals and tolerance
  - Liveness signal handling for online recovery
  - Shared poll response bus for efficiency
  - Detailed poll logging and metrics

#### Discovery & Device Management
- Multi-protocol unified discovery system
  - Automatic detection of Govee and LIFX devices
  - Protocol-specific discovery handlers
  - Capability enrichment from protocol catalogs
- Enhanced device metadata
  - LIFX: Firmware version, hardware version, label
  - Govee: Model-based capability lookup
  - Protocol-specific extension fields
- Manual device addition with protocol specification
  - Support for static IP devices
  - Protocol parameter: `--protocol {govee,lifx}`
  - Model number and device type specification

#### CLI Enhancements
- Multi-protocol device listing with protocol column
- Device command execution via CLI
  - `--on`/`--off` for native power control
  - `--brightness` (0-255) with automatic device power-on
  - `--color` (RGB hex) with shorthand support
  - `--kelvin` (0-255) with automatic range scaling
- Enhanced channel mapping display
  - Multi-universe support with dynamic IP lookup
  - Protocol-aware mapping validation
  - Improved mapping type terminology
- Interactive shell improvements
  - Device control subcommand
  - Better autocomplete and help text
  - Command input fixes

#### Logging & Debugging
- Protocol-specific loggers
  - `discovery.govee`, `discovery.lifx` for device discovery
  - `artnet.protocol`, `sacn.protocol` for DMX input
  - `protocol.govee`, `protocol.lifx` for device control
- Enhanced debug logging
  - Unmapped ArtNet/sACN packet logging
  - Detailed poll cycle logging
  - Discovery response logging
  - Priority merge decision logging
- Structured logging for device store operations

#### Developer Experience
- Added `uv.lock` for reproducible builds
- Comprehensive architecture documentation (`ARCHITECTURE_CHANGES.md`)
- Protocol handler development guide
- Enhanced test coverage for multi-protocol scenarios

### Changed

#### Architecture
- Refactored from monolithic to modular protocol architecture
- Separated DMX input protocols from device protocols
- Centralized mapping logic in `DmxMappingService`
- Simplified `ArtNetService` to input-only responsibilities
- Protocol-agnostic device management

#### Device Management
- Govee polling now routes shared responses to poller service
- Activity recovery gated to offline devices only
- Liveness signals reset offline state
- Enhanced last-seen tracking with protocol context

#### Configuration
- Expanded protocol configuration sections
- Per-protocol enable/disable flags
- Protocol-specific port and behavior settings
- Backward-compatible configuration migration

#### Installation & Packaging
- Updated Debian package to support multi-protocol installation
- Modified postinst/postrm scripts for new service names
- Updated systemd service files with new binary names
- Enhanced installation script for protocol detection

#### Documentation
- README updated with multi-protocol features
- INSTALL.md updated for new package name
- USAGE.md expanded with protocol-specific guides
- API documentation updated for new endpoints and events

### Fixed
- Brightness=0 turn-off behavior while preserving RGB controls
- Turn-off command sends only power command (not brightness)
- Dimmer field turn-off/on event handling
- Devices command help text and color command implementation
- Mapping type terminology in CLI help and autocomplete
- pytest failures in multi-protocol test suite
- LIFX device type storage with protocol filtering
- Polling tolerance logic for health tracking
- JSON syntax errors in LIFX capability catalog
- dpkg warning when removing `/opt` directory

### Deprecated
- None

### Removed
- Legacy command aliases (`artnet-lan-bridge`, `artnet-lan-cli`, `govee-artnet-bridge`, `govee-artnet-cli`)
- Direct Govee-specific naming from core codebase
- Monolithic discovery system (replaced with protocol-specific handlers)
- Hardcoded device type assumptions

## [1.0.3] - 2026-01-01

### Fixed
- Config validation timing - now validates after loading config file instead of before
- Capability catalog file ownership and permissions issues in postinst script
- Directory permissions for capability catalog to ensure proper access on Ubuntu 24.04
- Removed ACLs from capability catalog files to fix access issues on Ubuntu 24.04
- Systemd service check and file ownership issues during package installation
- typing-extensions dependency conflicts during Debian package installation

### Changed
- Added capability_catalog_path to system configuration for explicit path management
- Enhanced GitHub Actions workflow testing with proper sudo permissions for config file access
- Improved postinst script debugging and verification output

## [1.0.2] - 2026-01-01

### Added
- Debian package (.deb) build system for Ubuntu 24.04 and Debian 13
- GitHub Actions workflow for building and testing Debian packages
- Comprehensive configuration documentation (govee-bridge.toml.example)
- Automated pytest testing in package installation workflow
- Systemd service integration testing in CI
- Installation badges in README and INSTALL.md

### Changed
- Lowered dependency requirements to match Ubuntu 24.04 LTS packages
  - FastAPI: 0.110.0 → 0.101.0
  - httpx: 0.27.0 → 0.26.0
- Enhanced configuration file with extensive inline documentation
- Updated INSTALL.md with Debian package installation as primary method
- Improved python-app.yml workflow to use editable install

### Removed
- CAP_NET_BIND_SERVICE capability from systemd service (not needed for unprivileged ports)

## [1.0.1] - 2026-01-01

### Added
- WebSocket event notifications for device and health status changes
- GitHub Actions workflow for Python application
- requirements.txt file for dependency management

### Changed
- Updated API documentation for WebSocket events and mapping_count field

### Fixed
- Fixed failing API tests
- Removed unused global declarations in cli.py

## [1.0.0] - 2024-12-30

### Added
- Initial release
