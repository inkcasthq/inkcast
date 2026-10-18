// SPDX-License-Identifier: PolyForm-Shield-1.0.0
// Copyright 2026 ScalarOps Technologies LLC
// Starts Swagger UI from an external file so the page needs no inline script (strict CSP).
window.addEventListener("DOMContentLoaded", function () {
  var el = document.getElementById("swagger-ui");
  window.ui = SwaggerUIBundle({
    url: el.getAttribute("data-spec-url"),
    dom_id: "#swagger-ui",
    deepLinking: true,
    persistAuthorization: false,
    tryItOutEnabled: false,
    displayRequestDuration: true,
    validatorUrl: null, // never call the public swagger.io validator
  });
});
