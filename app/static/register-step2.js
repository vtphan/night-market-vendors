document.addEventListener("DOMContentLoaded", function () {
  var form = document.getElementById("discard-form");
  if (form) {
    form.addEventListener("submit", function (e) {
      if (!confirm("Discard this registration? All entered information will be lost.")) {
        e.preventDefault();
      }
    });
  }
});
