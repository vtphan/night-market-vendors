(function () {
  function timeAgo(dateStr) {
    var now = new Date();
    var then = new Date(dateStr);
    var seconds = Math.floor((now - then) / 1000);
    if (seconds < 60) return "just now";
    var minutes = Math.floor(seconds / 60);
    if (minutes < 60) return minutes + "m ago";
    var hours = Math.floor(minutes / 60);
    if (hours < 24) return hours + "h ago";
    var days = Math.floor(hours / 24);
    if (days < 30) return days + "d ago";
    return then.toLocaleDateString();
  }

  document.querySelectorAll("[data-ts]").forEach(function (el) {
    var ts = el.getAttribute("data-ts");
    el.textContent = timeAgo(ts);
    if (el.id === "last-reg-time") {
      var hours = (Date.now() - new Date(ts).getTime()) / 3600000;
      el.className = hours < 24 ? "fresh" : "stale";
    }
  });
})();
