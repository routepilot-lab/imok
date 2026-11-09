// Verbatim modal methods from OpenWrt LuCI ca805cb1595369af34fef31bd00f100ca197aca0
// Source: https://raw.githubusercontent.com/openwrt/luci/ca805cb1595369af34fef31bd00f100ca197aca0/modules/luci-base/htdocs/luci-static/resources/ui.js
// LuCI JavaScript is licensed under Apache-2.0; this test-only excerpt is not shipped.
module.exports = function(modalDiv, dom, document) { return {
	showModal(title, children, ...classes) {
		const dlg = modalDiv.firstElementChild;

		dlg.setAttribute('class', 'modal');
		dlg.classList.add(...classes);

		dom.content(dlg, dom.create('h4', {}, title));
		dom.append(dlg, children);

		document.body.classList.add('modal-overlay-active');
		modalDiv.scrollTop = 0;
		modalDiv.focus();

		return dlg;
	},
	hideModal() {
		document.body.classList.remove('modal-overlay-active');
		modalDiv.blur();
	},
	cancelModal(ev) {
		if (ev.key == 'Escape') {
			const btn = modalDiv.querySelector('.right > button, .right > .btn, .button-row > .btn');

			if (btn)
				btn.click();
		}
	},
}; };
