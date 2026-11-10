#!/bin/bash
# Called by aaPanel as "bash install.sh install|uninstall" when the plugin zip
# is imported or removed from the App Store.
PLUGIN_DIR=/www/server/panel/plugin/srvmon

case "$1" in
  install)
    # The release zip bundles the core under core/; installing from the git
    # repository puts the plugin in place after the core is installed.
    if [ -f "$PLUGIN_DIR/core/install.sh" ]; then
      bash "$PLUGIN_DIR/core/install.sh" --no-plugin
      rm -rf "$PLUGIN_DIR/core"
    fi
    echo "Successify"
    ;;
  uninstall)
    # Removing the plugin keeps the collector and its data. To remove those too:
    #   sudo /opt/srvmon/uninstall.sh [--purge]
    echo "Successify"
    ;;
esac
