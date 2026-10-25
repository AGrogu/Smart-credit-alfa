window.addEventListener('load', function () {
  window.ui = SwaggerUIBundle({
    validatorUrl: null,
    url: '/openapi.json', dom_id: '#swagger-ui', deepLinking: true,
    presets: [SwaggerUIBundle.presets.apis], layout: 'BaseLayout',
    requestInterceptor: async function (request) {
      request.credentials = 'same-origin';
      if (!['GET', 'HEAD'].includes((request.method || 'GET').toUpperCase()) && !request.url.endsWith('/auth/login')) {
        request.headers = request.headers || {};
        const response = await fetch('/api/auth/me');
        if (response.ok) request.headers['X-CSRF-Token'] = (await response.json()).csrf_token;
        request.headers['Idempotency-Key'] = crypto.randomUUID();
      }
      return request;
    }
  });
});
