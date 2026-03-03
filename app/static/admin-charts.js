(function () {
  var configEl = document.getElementById("chart-data");
  var chartData = configEl ? JSON.parse(configEl.textContent) : {};
  var dailyData = chartData.dailyCounts || [];
  var hourlyData = chartData.hourlyCounts || [];

  function drawBarChart(canvasId, labels, values, barColor) {
    var canvas = document.getElementById(canvasId);
    if (!canvas) return;
    var ctx = canvas.getContext("2d");
    var dpr = window.devicePixelRatio || 1;

    var rect = canvas.parentElement.getBoundingClientRect();
    canvas.width = rect.width * dpr;
    canvas.height = 180 * dpr;
    canvas.style.width = rect.width + "px";
    canvas.style.height = "180px";
    ctx.scale(dpr, dpr);

    var w = rect.width;
    var h = 180;
    var pad = { top: 20, right: 10, bottom: 35, left: 40 };
    var chartW = w - pad.left - pad.right;
    var chartH = h - pad.top - pad.bottom;

    var maxVal = Math.max.apply(null, values.concat([1]));
    var barW = Math.max(2, (chartW / values.length) - 2);
    var gap = (chartW - barW * values.length) / values.length;

    ctx.clearRect(0, 0, w, h);

    // Grid lines
    ctx.strokeStyle = "#e5e7eb";
    ctx.lineWidth = 0.5;
    var gridLines = 4;
    for (var i = 0; i <= gridLines; i++) {
      var y = pad.top + (chartH / gridLines) * i;
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(w - pad.right, y);
      ctx.stroke();
      ctx.fillStyle = "#9ca3af";
      ctx.font = "10px system-ui";
      ctx.textAlign = "right";
      ctx.fillText(Math.round(maxVal - (maxVal / gridLines) * i), pad.left - 5, y + 3);
    }

    // Bars
    values.forEach(function (val, idx) {
      var x = pad.left + idx * (barW + gap) + gap / 2;
      var barH = (val / maxVal) * chartH;
      var yPos = pad.top + chartH - barH;

      ctx.fillStyle = barColor;
      ctx.beginPath();
      var r = Math.min(3, barW / 2);
      ctx.moveTo(x, yPos + r);
      ctx.arcTo(x, yPos, x + r, yPos, r);
      ctx.arcTo(x + barW, yPos, x + barW, yPos + r, r);
      ctx.lineTo(x + barW, pad.top + chartH);
      ctx.lineTo(x, pad.top + chartH);
      ctx.closePath();
      ctx.fill();

      var labelEvery = Math.ceil(labels.length / 10);
      if (idx % labelEvery === 0) {
        ctx.fillStyle = "#9ca3af";
        ctx.font = "9px system-ui";
        ctx.textAlign = "center";
        ctx.save();
        ctx.translate(x + barW / 2, h - 5);
        ctx.rotate(-0.5);
        ctx.fillText(labels[idx], 0, 0);
        ctx.restore();
      }
    });
  }

  function renderDaily() {
    if (!dailyData.length) return;
    var labels = dailyData.map(function (d) { return d.date; });
    var values = dailyData.map(function (d) { return d.count; });
    drawBarChart("daily-chart", labels, values, "#6366f1");
  }

  function renderHourly() {
    if (!hourlyData.length) return;
    var labels = hourlyData.map(function (_, i) {
      var hr = i % 12 || 12;
      return hr + (i < 12 ? "a" : "p");
    });
    drawBarChart("hourly-chart", labels, hourlyData, "#8b5cf6");
  }

  function showChart(type) {
    document.getElementById("chart-daily").style.display = type === "daily" ? "" : "none";
    document.getElementById("chart-hourly").style.display = type === "hourly" ? "" : "none";
    if (type === "daily") renderDaily();
    else renderHourly();
  }

  document.querySelectorAll("[data-chart-tab]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      showChart(btn.dataset.chartTab);
      document.querySelectorAll("[data-chart-tab]").forEach(function (t) {
        t.classList.remove("active");
      });
      btn.classList.add("active");
    });
  });

  setTimeout(renderDaily, 50);
})();
