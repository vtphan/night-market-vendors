document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll("tr[data-href]").forEach(function (row) {
    row.addEventListener("click", function () {
      window.location = row.getAttribute("data-href");
    });
  });
});
