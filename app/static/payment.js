(function () {
  var form = document.getElementById("payment-form");
  if (!form) return;
  var publishableKey = form.dataset.publishableKey;
  var registrationId = form.dataset.registrationId;
  var csrfToken = form.dataset.csrfToken;
  if (!publishableKey) return;

  var stripe = Stripe(publishableKey);
  var elements = stripe.elements();
  var cardElement = elements.create("card");
  cardElement.mount("#card-element");

  var button = document.getElementById("pay-button");
  var buttonText = button.textContent;
  var errorDisplay = document.getElementById("card-errors");
  var statusMsg = document.getElementById("payment-status-msg");

  function showStatus(text, color) {
    statusMsg.textContent = text;
    statusMsg.style.color = color || "var(--pico-muted-color)";
    statusMsg.style.display = "block";
  }

  function hideStatus() {
    statusMsg.style.display = "none";
  }

  cardElement.on("change", function (event) {
    errorDisplay.textContent = event.error ? event.error.message : "";
  });

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "Processing...";
    errorDisplay.textContent = "";
    showStatus("Please do not close or refresh this page.");

    fetch("/vendor/registration/" + registrationId + "/pay", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: "csrf_token=" + encodeURIComponent(csrfToken),
    })
      .then(function (response) {
        return response.json().then(function (data) {
          if (!response.ok) {
            throw new Error(data.error || "Payment service error. Please try again.");
          }
          return data;
        });
      })
      .then(function (data) {
        if (data.error) {
          throw new Error(data.error);
        }
        showStatus("Verifying your card with the bank. Please do not close or refresh this page.");
        return stripe.confirmCardPayment(data.client_secret, {
          payment_method: { card: cardElement },
        });
      })
      .then(function (result) {
        if (result.error) {
          throw new Error(result.error.message);
        }
        // Payment confirmed — show success state and prevent interaction
        // while we wait briefly for the webhook to process.
        button.textContent = "Payment confirmed! Redirecting\u2026";
        button.classList.add("payment-success");
        button.removeAttribute("aria-busy");
        errorDisplay.textContent = "";
        cardElement.update({ disabled: true });
        showStatus("Your payment was successful. You will be redirected shortly.", "#155724");

        // Brief delay gives the webhook a head start before the vendor
        // lands on the registration page and sees the updated status.
        setTimeout(function () {
          window.location.href = "/vendor/registration/" + registrationId;
        }, 3000);
      })
      .catch(function (error) {
        errorDisplay.textContent = error.message;
        button.disabled = false;
        button.removeAttribute("aria-busy");
        button.textContent = buttonText;
        hideStatus();
      });
  });
})();
