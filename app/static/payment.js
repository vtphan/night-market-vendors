(function () {
  var configEl = document.getElementById("payment-config");
  if (!configEl) return;
  var config = JSON.parse(configEl.textContent);
  if (!config || !config.publishableKey) return;

  var stripe = Stripe(config.publishableKey);
  var elements = stripe.elements();
  var cardElement = elements.create("card");
  cardElement.mount("#card-element");

  var form = document.getElementById("payment-form");
  var button = document.getElementById("pay-button");
  var buttonText = button.textContent;
  var errorDisplay = document.getElementById("card-errors");

  cardElement.on("change", function (event) {
    errorDisplay.textContent = event.error ? event.error.message : "";
  });

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "Processing...";
    errorDisplay.textContent = "";

    fetch("/vendor/registration/" + config.registrationId + "/pay", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: "csrf_token=" + encodeURIComponent(config.csrfToken),
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("Payment service error. Please try again.");
        }
        return response.json();
      })
      .then(function (data) {
        if (data.error) {
          throw new Error(data.error);
        }
        return stripe.confirmCardPayment(data.client_secret, {
          payment_method: { card: cardElement },
        });
      })
      .then(function (result) {
        if (result.error) {
          throw new Error(result.error.message);
        }
        window.location.href = "/vendor/dashboard";
      })
      .catch(function (error) {
        errorDisplay.textContent = error.message;
        button.disabled = false;
        button.removeAttribute("aria-busy");
        button.textContent = buttonText;
      });
  });
})();
