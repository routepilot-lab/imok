include $(TOPDIR)/rules.mk

PKG_NAME:=luci-app-routepilot
PKG_VERSION:=0.2.36
PKG_RELEASE:=1
PKG_LICENSE:=MIT GPL-2.0
PKG_MAINTAINER:=imok contributors

include $(INCLUDE_DIR)/package.mk

define Package/luci-app-routepilot
  SECTION:=luci
  CATEGORY:=LuCI
  SUBMENU:=3. Applications
  TITLE:=imOK MASQUE per-device routing
  PKGARCH:=all
  DEPENDS:=+luci-base +python3-light +python3-urllib +python3-logging +python3-ctypes +python3-openssl +python3-uuid +python3-codecs +openssl-util +ip-full +nftables-json +kmod-tun +kmod-veth +conntrack +curl +ca-bundle
endef

define Package/luci-app-routepilot/conffiles
/etc/config/routepilot
endef

define Build/Compile
	printf '%s\n' '{"product":"$(PKG_VERSION)","package":"$(PKG_VERSION)-$(PKG_RELEASE)"}' > ./root/usr/share/routepilot/version.json
	python3 ./tools/asset_versions.py --check
endef

define Package/luci-app-routepilot/postinst
#!/bin/sh
[ -n "$${IPKG_INSTROOT}" ] && exit 0
chmod 600 /etc/config/routepilot
rm -f /tmp/luci-indexcache /tmp/luci-indexcache.*.json
/etc/init.d/rpcd reload
/etc/init.d/routepilot enable
/etc/init.d/routepilot start
exit 0
endef

define Package/luci-app-routepilot/prerm
#!/bin/sh
[ -n "$${IPKG_INSTROOT}" ] && exit 0
/etc/init.d/routepilot stop
/usr/libexec/routepilot cleanup
/etc/init.d/routepilot disable
exit 0
endef

define Package/luci-app-routepilot/install
	$(CP) ./root/* $(1)/
	$(RM) -r $(1)/usr/lib/routepilot/__pycache__
	$(INSTALL_DIR) $(1)/www
	$(CP) ./htdocs/* $(1)/www/
	chmod 600 $(1)/etc/config/routepilot
	chmod 755 $(1)/etc/init.d/routepilot $(1)/usr/libexec/routepilot $(1)/usr/libexec/routepilot-exits $(1)/usr/libexec/routepilot-observe $(1)/usr/libexec/routepilot-traffic $(1)/usr/libexec/routepilot-backup-list $(1)/usr/libexec/routepilot-clients $(1)/usr/libexec/routepilot-clients-list
endef

$(eval $(call BuildPackage,luci-app-routepilot))
