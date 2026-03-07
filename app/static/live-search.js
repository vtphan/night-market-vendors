// Auto-submit dropdown filters on change
(function () {
  var form = document.getElementById("filter-form");
  if (!form) return;
  var selects = form.querySelectorAll("select");
  for (var i = 0; i < selects.length; i++) {
    selects[i].addEventListener("change", function () { form.submit(); });
  }
})();

// Live search: filter table rows client-side
(function () {
  var input = document.getElementById("live-search-input");
  var table = document.getElementById("registrations-table");
  if (!input || !table) return;

  var tbody = table.querySelector("tbody");
  var rows = tbody ? Array.from(tbody.rows) : [];
  var timer;

  input.addEventListener("input", function () {
    clearTimeout(timer);
    timer = setTimeout(filter, 150);
  });

  function filter() {
    var query = input.value.trim().toLowerCase();
    for (var i = 0; i < rows.length; i++) {
      var text = rows[i].textContent.toLowerCase();
      rows[i].style.display = !query || text.indexOf(query) !== -1 ? "" : "none";
    }
  }
})();
