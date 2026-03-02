(function () {
  document.querySelectorAll("[data-open-dialog]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var dialog = document.getElementById(btn.dataset.openDialog);
      if (dialog) dialog.showModal();
    });
  });

  document.querySelectorAll("[data-close-dialog]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      btn.closest("dialog").close();
    });
  });
})();
