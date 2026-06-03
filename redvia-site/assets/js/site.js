(function () {
  var SITE_BASE = location.pathname.indexOf('/ingredients/') > -1 ? '../' : '';
  var TRACK_ENDPOINT = '/api/event';
  var IDENTIFY_ENDPOINT = '/api/identify';
  var pageStartedAt = Date.now();
  var sentDwell = {};

  function getClientId() {
    var key = 'rv_client_id';
    try {
      var existing = localStorage.getItem(key);
      if (existing) return existing;
      var id = 'rv_' + Math.random().toString(36).slice(2) + Date.now().toString(36);
      localStorage.setItem(key, id);
      return id;
    } catch (e) {
      return 'rv_' + Math.random().toString(36).slice(2);
    }
  }

  function readTokenFromUrl() {
    var params = new URLSearchParams(location.search);
    return params.get('t') || params.get('token') || params.get('rv_token') || '';
  }

  function removeTokenFromUrl() {
    if (!window.history || !history.replaceState) return;
    var url = new URL(location.href);
    ['t', 'token', 'rv_token'].forEach(function (key) { url.searchParams.delete(key); });
    history.replaceState(null, document.title, url.pathname + url.search + url.hash);
  }

  function postJson(endpoint, body, preferBeacon) {
    body.client_id = getClientId();
    body.url = location.href;
    body.page_path = location.pathname;
    body.page_title = document.title;
    body.referrer = document.referrer || '';

    var json = JSON.stringify(body);
    if (preferBeacon && navigator.sendBeacon) {
      try {
        return navigator.sendBeacon(endpoint, new Blob([json], { type: 'application/json' }));
      } catch (e) {}
    }

    if (!window.fetch) return false;
    try {
      fetch(endpoint, {
        method: 'POST',
        credentials: 'include',
        keepalive: !!preferBeacon,
        headers: { 'Content-Type': 'application/json' },
        body: json
      }).catch(function () {});
      return true;
    } catch (e) {
      return false;
    }
  }

  function track(eventType, payload, preferBeacon) {
    return postJson(TRACK_ENDPOINT, {
      event_type: eventType,
      payload: payload || {}
    }, preferBeacon);
  }

  function identifyToken(token) {
    if (!token || !window.fetch) return Promise.resolve(false);
    return fetch(IDENTIFY_ENDPOINT, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        token: token,
        visitor_id: getClientId(),
        url: location.href,
        page_title: document.title
      })
    }).then(function () {
      removeTokenFromUrl();
      return true;
    }).catch(function () {
      return false;
    });
  }

  function productSlugFromPath(path) {
    var match = path.match(/\/ingredients\/([^/]+)\.html$/);
    return match ? match[1] : '';
  }

  function linkProductSlug(href) {
    var match = href.match(/ingredients\/([^/#?]+)\.html/);
    return match ? match[1] : '';
  }

  function linkLabel(link) {
    return (link.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 160);
  }

  function setupTrackingNotice() {
    var footer = document.querySelector('.footer__bottom');
    if (!footer || footer.querySelector('.footer__tracking-note')) return;
    var note = document.createElement('span');
    note.className = 'footer__tracking-note';
    note.textContent = 'We use first-party analytics to understand which products buyers care about.';
    footer.appendChild(note);
  }

  function ensureModal() {
    var m = document.getElementById('cert-modal');
    if (m) return m;
    m = document.createElement('div');
    m.id = 'cert-modal';
    m.className = 'cert-modal';
    m.innerHTML =
      '<div class="cert-modal__frame" role="dialog" aria-modal="true">' +
      '<button type="button" class="cert-modal__close" aria-label="Close">×</button>' +
      '<img class="cert-modal__img" alt="">' +
      '<p class="cert-modal__title"></p>' +
      '</div>';
    document.body.appendChild(m);
    m.addEventListener('click', function (e) { if (e.target === m) closeModal(); });
    m.querySelector('.cert-modal__close').addEventListener('click', closeModal);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && m.classList.contains('is-open')) closeModal();
    });
    return m;
  }

  function openModal(cert, name) {
    var m = ensureModal();
    var img = m.querySelector('.cert-modal__img');
    img.src = SITE_BASE + 'assets/img/certs/' + cert + '.jpg';
    img.alt = name;
    m.querySelector('.cert-modal__title').textContent = name;
    m.classList.add('is-open');
    document.body.style.overflow = 'hidden';
  }

  function closeModal() {
    var m = document.getElementById('cert-modal');
    if (m) m.classList.remove('is-open');
    document.body.style.overflow = '';
  }

  document.addEventListener('click', function (e) {
    var btn = e.target.closest && e.target.closest('.cert-badge[data-cert]');
    if (btn) {
      e.preventDefault();
      openModal(btn.dataset.cert, btn.dataset.name || 'Certificate');
      track('cert_open', {
        cert: btn.dataset.cert || '',
        name: btn.dataset.name || btn.textContent || ''
      });
    }
  });

  document.addEventListener('click', function (e) {
    var link = e.target.closest && e.target.closest('a[href]');
    if (!link) return;
    var href = link.getAttribute('href') || '';

    if (href.indexOf('mailto:') === 0) {
      track('mailto_click', { email: href.replace(/^mailto:/, '').split('?')[0], label: linkLabel(link) }, true);
      return;
    }

    if (href.indexOf('tel:') === 0) {
      track('tel_click', { phone: href.replace(/^tel:/, ''), label: linkLabel(link) }, true);
      return;
    }

    var productSlug = linkProductSlug(href);
    if (productSlug) {
      var family = link.closest('.product-family-card');
      track(family ? 'product_family_click' : 'product_click', {
        slug: productSlug,
        label: linkLabel(link),
        family: family ? linkLabel(family.querySelector('.product-family-card__kicker') || family) : ''
      }, true);
      return;
    }

    if (href.indexOf('contact.html') > -1 || link.classList.contains('btn')) {
      track('cta_click', { href: href, label: linkLabel(link) }, true);
    }
  });

  function setupContactForm() {
    var form = document.getElementById('contact-form');
    if (!form) return;

    function fieldValue(id) {
      var el = document.getElementById(id);
      return el && typeof el.value === 'string' ? el.value : '';
    }

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var name = fieldValue('name');
      var company = fieldValue('company');
      var email = fieldValue('email');
      var country = fieldValue('country');
      var brief = fieldValue('brief');
      var emailDomain = email.indexOf('@') > -1 ? email.split('@').pop() : '';

      track('form_submit', {
        has_name: !!name,
        has_email: !!email,
        has_brief: !!brief,
        company: company.slice(0, 160),
        email_domain: emailDomain,
        country: country.slice(0, 120),
        brief_length: brief.length
      }, true);

      var subject = encodeURIComponent('Redvia brief — ' + (company || name || 'inquiry'));
      var body = encodeURIComponent(brief);
      window.setTimeout(function () {
        window.location.href = 'mailto:trade@redvia-bry.com?subject=' + subject + '&body=' + body;
      }, 120);
    });
  }

  function sendPageEvents() {
    track('page_view', {});
    var slug = productSlugFromPath(location.pathname);
    if (slug) {
      track('product_view', {
        slug: slug,
        title: (document.querySelector('.prod-hero__name') || {}).textContent || document.title
      });
    }
  }

  function sendDwellEvent(type, forceBeacon) {
    if (sentDwell[type]) return;
    sentDwell[type] = true;
    track(type, {
      dwell_ms: Date.now() - pageStartedAt,
      slug: productSlugFromPath(location.pathname)
    }, forceBeacon);
  }

  function setupTracking() {
    setupTrackingNotice();
    setupContactForm();

    var token = readTokenFromUrl();
    if (token) {
      identifyToken(token).then(sendPageEvents);
    } else {
      sendPageEvents();
    }

    window.setTimeout(function () { sendDwellEvent('dwell_30s', false); }, 30000);
    window.setTimeout(function () { sendDwellEvent('dwell_60s', false); }, 60000);
    window.addEventListener('pagehide', function () {
      track('dwell', {
        dwell_ms: Date.now() - pageStartedAt,
        slug: productSlugFromPath(location.pathname)
      }, true);
    });
  }

  function setupAnimations() {
    var targets = document.querySelectorAll(
      '.section, .prod-hero, .science, .cta, .hero, .product-visual, .origin-block, .story, .cert-strip, .prod-nextnav, .contact-callout, .addr-grid, .split-copy, .form'
    );
    if (!targets.length) return;
    targets.forEach(function (el) { el.setAttribute('data-animate', ''); });
    if (!('IntersectionObserver' in window) || window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      targets.forEach(function (el) { el.classList.add('is-visible'); });
      return;
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (ent) {
        if (ent.isIntersecting) {
          ent.target.classList.add('is-visible');
          io.unobserve(ent.target);
        }
      });
    }, { threshold: 0.08, rootMargin: '0px 0px -8% 0px' });
    targets.forEach(function (el) { io.observe(el); });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      setupAnimations();
      setupTracking();
    });
  } else {
    setupAnimations();
    setupTracking();
  }

  window.redviaTrack = track;
})();
