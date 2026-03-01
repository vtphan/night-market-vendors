(function () {
  var configEl = document.getElementById("admin-reg-config");
  var config = configEl ? JSON.parse(configEl.textContent) : {};
  var ADMIN_EMAIL = config.adminEmail || "";
  var BOOTH_FEE_CENTS = config.boothFeeCents || 0;

  // --- Refund amount calculator ---
  function updateRefundAmount() {
    var sel = document.getElementById("refund-percent-select");
    if (!sel) return;
    var pct = parseFloat(sel.value) || 0;
    var refundCents = Math.round(BOOTH_FEE_CENTS * pct / 100);
    var refundDollars = (refundCents / 100).toFixed(2);
    document.getElementById("refund-amount-hidden").value = refundDollars;
    var boothDollars = (BOOTH_FEE_CENTS / 100).toFixed(2);
    document.getElementById("refund-calc-display").textContent =
      pct + "% of $" + boothDollars + " = $" + refundDollars;
  }

  document.addEventListener("DOMContentLoaded", updateRefundAmount);

  // Refund percentage selector
  var refundSel = document.getElementById("refund-percent-select");
  if (refundSel) {
    refundSel.addEventListener("change", updateRefundAmount);
  }

  // --- Custom reason toggle ---
  function toggleCustomReason(prefix) {
    var sel = document.getElementById(prefix + "-reason-select");
    var custom = document.getElementById(prefix + "-reason-custom");
    custom.style.display = sel.value === "__other__" ? "" : "none";
    if (sel.value !== "__other__") custom.value = "";
  }

  // Bind reason selects via data attribute
  document.querySelectorAll("[data-reason-select]").forEach(function (sel) {
    sel.addEventListener("change", function () {
      toggleCustomReason(sel.dataset.reasonSelect);
    });
  });

  // --- Reversal submit preparation ---
  function prepareReversalSubmit(prefix) {
    var sel = document.getElementById(prefix + "-reason-select");
    var custom = document.getElementById(prefix + "-reason-custom");
    var hidden = document.getElementById(prefix + "-reason-hidden");
    var nameInput = document.getElementById(prefix + "-admin-name");
    if (!sel.value) { alert("Please select a reason."); return false; }
    var name = nameInput.value.trim();
    if (!name) { alert("Please enter your name."); nameInput.focus(); return false; }
    var reason;
    if (sel.value === "__other__") {
      if (!custom.value.trim()) { alert("Please enter a custom reason."); return false; }
      reason = custom.value.trim();
    } else {
      reason = sel.value;
    }
    hidden.value = reason + " \u2014 " + name + " (" + ADMIN_EMAIL + ")";
    return true;
  }

  // Bind reversal forms via data attribute
  document.querySelectorAll("[data-reversal-prefix]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!prepareReversalSubmit(form.dataset.reversalPrefix)) {
        e.preventDefault();
      }
    });
  });

  // --- Approve confirmation ---
  document.querySelectorAll("[data-confirm-approve]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!confirm("Approve this registration?\n\nThe vendor (" + form.dataset.vendorEmail + ") will be notified by email with a payment link.")) {
        e.preventDefault();
      }
    });
  });

  // --- Dialog openers ---
  document.querySelectorAll("[data-open-dialog]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      document.getElementById(btn.dataset.openDialog).showModal();
    });
  });

  // --- Dialog closers ---
  document.querySelectorAll("[data-close-dialog]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      btn.closest("dialog").close();
    });
  });
})();
