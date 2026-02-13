/* ============================================================
   SENNS - Minimal JS · Navigation, Mobile Menu, FAQ
   ============================================================ */

(function () {
  'use strict';

  // --- Navigation scroll effect (only on home page) ---
  const nav = document.getElementById('nav');
  if (nav && !nav.classList.contains('is-scrolled')) {
    let ticking = false;
    window.addEventListener('scroll', function () {
      if (!ticking) {
        window.requestAnimationFrame(function () {
          if (window.scrollY > 60) {
            nav.classList.add('is-scrolled');
          } else {
            nav.classList.remove('is-scrolled');
          }
          ticking = false;
        });
        ticking = true;
      }
    });
  }

  // --- Mobile menu toggle ---
  const burger = document.getElementById('burger');
  const mobileMenu = document.getElementById('mobileMenu');

  if (burger && mobileMenu) {
    burger.addEventListener('click', function () {
      const isOpen = mobileMenu.classList.toggle('is-open');
      burger.setAttribute('aria-expanded', isOpen);

      // Animate burger to X
      const spans = burger.querySelectorAll('span');
      if (isOpen) {
        spans[0].style.transform = 'translateY(3px) rotate(45deg)';
        spans[1].style.transform = 'translateY(-3px) rotate(-45deg)';
        document.body.style.overflow = 'hidden';
      } else {
        spans[0].style.transform = '';
        spans[1].style.transform = '';
        document.body.style.overflow = '';
      }
    });

    // Close on link click
    mobileMenu.querySelectorAll('a').forEach(function (link) {
      link.addEventListener('click', function () {
        mobileMenu.classList.remove('is-open');
        burger.querySelectorAll('span').forEach(function (s) { s.style.transform = ''; });
        document.body.style.overflow = '';
      });
    });
  }

  // --- FAQ accordion ---
  const faqItems = document.querySelectorAll('.faq-item');
  faqItems.forEach(function (item) {
    const question = item.querySelector('.faq-question');
    if (question) {
      question.addEventListener('click', function () {
        const wasOpen = item.classList.contains('is-open');

        // Close all
        faqItems.forEach(function (i) {
          i.classList.remove('is-open');
          var q = i.querySelector('.faq-question');
          if (q) q.setAttribute('aria-expanded', 'false');
        });

        // Toggle current
        if (!wasOpen) {
          item.classList.add('is-open');
          question.setAttribute('aria-expanded', 'true');
        }
      });
    }
  });

  // --- Language switcher ---
  document.querySelectorAll('.nav__lang-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var lang = btn.dataset.lang;
      var path = window.location.pathname;

      // Map PL filenames to EN filenames
      var plToEn = {
        'index.html': 'index.html',
        'portfolio.html': 'portfolio.html',
        'projekty.html': 'projects.html',
        'o-nas.html': 'about.html',
        'faq.html': 'faq.html',
        'privacy.html': 'privacy.html',
        'terms.html': 'terms.html',
        'technical.html': 'technical.html',
        'licensing.html': 'licensing.html'
      };

      // Reverse map
      var enToPl = {};
      for (var key in plToEn) {
        enToPl[plToEn[key]] = key;
      }

      var isInEn = path.indexOf('/en/') !== -1;
      var filename = path.split('/').pop() || 'index.html';

      if (lang === 'en' && !isInEn) {
        var enFile = plToEn[filename] || filename;
        window.location.href = '/en/' + enFile;
      } else if (lang === 'pl' && isInEn) {
        var plFile = enToPl[filename] || filename;
        window.location.href = '/' + plFile;
      }
    });
  });

})();
