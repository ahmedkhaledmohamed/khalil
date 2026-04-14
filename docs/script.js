// Mobile nav toggle
const navToggle = document.querySelector('.k-nav-toggle');
const navLinksContainer = document.querySelector('.k-nav-links');

if (navToggle && navLinksContainer) {
    navToggle.addEventListener('click', () => {
        navToggle.classList.toggle('active');
        navLinksContainer.classList.toggle('open');
    });
}

// Smooth scroll for section nav anchors
document.querySelectorAll('a[href^="#"]').forEach(anchor => {
    anchor.addEventListener('click', function (e) {
        e.preventDefault();
        if (navToggle) navToggle.classList.remove('active');
        if (navLinksContainer) navLinksContainer.classList.remove('open');
        const target = document.querySelector(this.getAttribute('href'));
        if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
});

// Scroll spy for section nav (highlight current section)
const sections = document.querySelectorAll('section[id]');
const sectionNavLinks = document.querySelectorAll('.section-nav a');

if (sectionNavLinks.length > 0) {
    window.addEventListener('scroll', () => {
        let current = '';
        sections.forEach(section => {
            if (scrollY >= section.offsetTop - 200) current = section.getAttribute('id');
        });
        sectionNavLinks.forEach(link => {
            link.classList.remove('active');
            if (link.getAttribute('href') === `#${current}`) link.classList.add('active');
        });
    });
}

// Animate elements on scroll
const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
        if (entry.isIntersecting) entry.target.classList.add('visible');
    });
}, { threshold: 0.1, rootMargin: '0px 0px -40px 0px' });

document.querySelectorAll(
    '.cap-card, .evo-step, .signal-card, .tier-card, .stack-item, .arch-layer, .signal-examples h3'
).forEach(el => {
    el.classList.add('animate-in');
    observer.observe(el);
});
