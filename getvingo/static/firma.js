// Riquadro firma col dito/mouse. Ogni .firma-pad scrive un PNG (data URL) nel suo input nascosto.
(function () {
  document.querySelectorAll('.firma-pad').forEach(function (box) {
    var canvas = box.querySelector('canvas');
    var input = box.querySelector('input[type=hidden]');
    var cancella = box.querySelector('[data-cancella]');
    var ctx = canvas.getContext('2d');
    var disegnando = false, vuota = true, ultimo = null;

    function dimensiona() {
      var r = canvas.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
      canvas.width = r.width * dpr; canvas.height = r.height * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.lineWidth = 2.4; ctx.lineCap = 'round'; ctx.lineJoin = 'round'; ctx.strokeStyle = '#1C1533';
      vuota = true; input.value = ''; box.classList.remove('firmata');
    }
    function punto(e) {
      var r = canvas.getBoundingClientRect();
      return { x: e.clientX - r.left, y: e.clientY - r.top };
    }
    canvas.addEventListener('pointerdown', function (e) {
      disegnando = true; ultimo = punto(e); canvas.setPointerCapture(e.pointerId); e.preventDefault();
    });
    canvas.addEventListener('pointermove', function (e) {
      if (!disegnando) return;
      var p = punto(e);
      ctx.beginPath(); ctx.moveTo(ultimo.x, ultimo.y); ctx.lineTo(p.x, p.y); ctx.stroke();
      ultimo = p; vuota = false;
    });
    // la firma si salva a 600 px di larghezza: leggera anche con un timbro sotto
    function salva() {
      var largo = 600, c = document.createElement('canvas');
      c.width = largo; c.height = Math.round(canvas.height * largo / canvas.width);
      c.getContext('2d').drawImage(canvas, 0, 0, c.width, c.height);
      input.value = c.toDataURL('image/png'); box.classList.add('firmata');
    }
    function fine() {
      if (!disegnando) return;
      disegnando = false;
      if (!vuota) salva();
    }
    // logo o timbro dell'azienda sotto la firma: si adatta al riquadro, poi si firma sopra
    var campoTimbro = box.parentElement.querySelector('[data-timbro]');
    if (campoTimbro) campoTimbro.addEventListener('change', function () {
      var file = campoTimbro.files[0];
      if (!file) return;
      var img = new Image();
      img.onload = function () {
        dimensiona();
        var r = canvas.getBoundingClientRect(), margine = 8;
        var scala = Math.min((r.width - 2 * margine) / img.width, (r.height - 2 * margine) / img.height);
        var w = img.width * scala, h = img.height * scala;
        ctx.globalAlpha = 0.9; ctx.drawImage(img, (r.width - w) / 2, (r.height - h) / 2, w, h); ctx.globalAlpha = 1;
        vuota = false; salva();
        URL.revokeObjectURL(img.src);
      };
      img.src = URL.createObjectURL(file);
      campoTimbro.value = '';
    });
    canvas.addEventListener('pointerup', fine);
    canvas.addEventListener('pointercancel', fine);
    if (cancella) cancella.addEventListener('click', function () { dimensiona(); });

    var form = box.closest('form');
    if (form && box.dataset.obbligatoria !== undefined) {
      form.addEventListener('submit', function (e) {
        // il bottone "predisponi" non richiede firma
        var invio = e.submitter;
        if (invio && invio.dataset.senzaFirma !== undefined) return;
        if (!input.value) {
          e.preventDefault(); e.stopImmediatePropagation();
          box.classList.add('manca');
          box.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
      }, true);
    }
    dimensiona();
    window.addEventListener('resize', function () { if (vuota) dimensiona(); });
  });
})();

// 22/11/2026: timbro della società da solo (Dati aziendali, fornitori, luoghi): ridotto a 600 px, in PNG
(function () {
  document.querySelectorAll('[data-carica-timbro]').forEach(function (campo) {
    var box = campo.closest('.campo-timbro'), uscita = box.querySelector('input[name=timbro]'),
        anteprima = box.querySelector('[data-timbro-anteprima]');
    campo.addEventListener('change', function () {
      var file = campo.files[0]; if (!file) return;
      var img = new Image();
      img.onload = function () {
        var scala = Math.min(1, 600 / Math.max(img.width, img.height)), c = document.createElement('canvas');
        c.width = Math.round(img.width * scala); c.height = Math.round(img.height * scala);
        c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
        uscita.value = c.toDataURL('image/png');
        anteprima.innerHTML = '<img alt="Nuovo timbro">'; anteprima.querySelector('img').src = uscita.value;
        URL.revokeObjectURL(img.src);
      };
      img.src = URL.createObjectURL(file);
      campo.value = '';
    });
  });
})();

// 22/11/2026: firma col dito a tutto schermo (_macro.firma_dal_vivo). All'invio di un modulo di partenza o di
// arrivo si apre la pagina della firma, con sotto il timbro della società: senza firma il modulo non parte.
// La firma (timbro + tratto) si salva a 600 px in PNG nel campo "firma" e il modulo prosegue da solo.
(function () {
  var campi = document.querySelectorAll('input[data-firma-vivo]');
  if (!campi.length) return;
  var foglio = document.createElement('div');
  foglio.className = 'foglio-firma'; foglio.hidden = true;
  foglio.setAttribute('role', 'dialog'); foglio.setAttribute('aria-modal', 'true');
  foglio.innerHTML =
    '<div class="foglio-firma-testa"><div><b data-f-titolo></b><small data-f-sotto></small></div>' +
    '<button type="button" class="foglio-firma-chiudi" data-f-annulla aria-label="Annulla">✕</button></div>' +
    '<div class="foglio-firma-area"><div class="foglio-firma-carta"><img alt="" data-f-timbro hidden><canvas aria-label="Firma qui col dito"></canvas>' +
    '<span class="foglio-firma-guida" data-f-guida>Firma qui col dito</span></div>' +
    '<p class="foglio-firma-nota" data-f-nota></p></div>' +
    '<div class="foglio-firma-piede"><button type="button" class="btn" data-f-cancella>Cancella</button>' +
    '<button type="button" class="btn" data-f-salta hidden>Continua senza firma</button>' +
    '<button type="button" class="btn primario" data-f-ok disabled>Conferma la firma</button></div>';
  document.body.appendChild(foglio);
  var canvas = foglio.querySelector('canvas'), ctx = canvas.getContext('2d'), timbroImg = foglio.querySelector('[data-f-timbro]');
  var ok = foglio.querySelector('[data-f-ok]'), guida = foglio.querySelector('[data-f-guida]');
  var attuale = null, invio = null, disegnando = false, vuota = true, ultimo = null;

  function dimensiona() {
    var r = canvas.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    canvas.width = r.width * dpr; canvas.height = r.height * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.lineWidth = 3; ctx.lineCap = 'round'; ctx.lineJoin = 'round'; ctx.strokeStyle = '#11171D';
    vuota = true; ok.disabled = true; guida.hidden = false;
  }
  function punto(e) { var r = canvas.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; }
  canvas.addEventListener('pointerdown', function (e) { disegnando = true; ultimo = punto(e); canvas.setPointerCapture(e.pointerId); e.preventDefault(); });
  canvas.addEventListener('pointermove', function (e) {
    if (!disegnando) return;
    var p = punto(e);
    ctx.beginPath(); ctx.moveTo(ultimo.x, ultimo.y); ctx.lineTo(p.x, p.y); ctx.stroke();
    ultimo = p; vuota = false; ok.disabled = false; guida.hidden = true;
  });
  ['pointerup', 'pointercancel'].forEach(function (n) { canvas.addEventListener(n, function () { disegnando = false; }); });
  foglio.querySelector('[data-f-cancella]').addEventListener('click', dimensiona);

  function apri(campo, bottone) {
    attuale = campo; invio = bottone;
    foglio.querySelector('[data-f-titolo]').textContent = campo.dataset.titolo || 'Firma';
    foglio.querySelector('[data-f-sotto]').textContent = [campo.dataset.chi, campo.dataset.societa].filter(Boolean).join(' · ');
    var t = campo.dataset.timbro;
    timbroImg.hidden = !t; if (t) timbroImg.src = t;
    foglio.querySelector('[data-f-nota]').textContent = t ? 'Firma sopra il timbro della società.' :
      campo.hasAttribute('data-bianco') ? 'Firma su spazio bianco: il timbro della società è già nel mittente.' :
      (campo.dataset.societa ? 'Timbro di ' + campo.dataset.societa + ' non ancora caricato: si firma senza.' : '');
    // test (Gestione → Firme facoltative): si può proseguire senza firmare
    foglio.querySelector('[data-f-salta]').hidden = campo.dataset.facoltativa === undefined;
    foglio.querySelector('.foglio-firma-piede').classList.toggle('con-salta', campo.dataset.facoltativa !== undefined);
    foglio.hidden = false; document.documentElement.classList.add('con-foglio-firma');
    requestAnimationFrame(dimensiona);
  }
  function chiudi() { foglio.hidden = true; document.documentElement.classList.remove('con-foglio-firma'); }
  foglio.querySelector('[data-f-annulla]').addEventListener('click', function () {
    chiudi();
    var form = attuale && attuale.form;
    if (form) { form.dataset.firmaAnnullata = '1'; if (invio) invio.classList.remove('in-corso'); }
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !foglio.hidden) foglio.querySelector('[data-f-annulla]').click(); });

  foglio.querySelector('[data-f-salta]').addEventListener('click', function () {
    if (!attuale) return;
    attuale.value = ''; attuale.dataset.saltata = '1';
    var form = attuale.form, bottone = invio;
    chiudi();
    if (form.requestSubmit) form.requestSubmit(bottone && bottone.form === form ? bottone : undefined); else form.submit();
  });

  ok.addEventListener('click', function () {
    if (vuota || !attuale) return;
    // timbro sotto, tratto sopra: un'immagine sola da 600 px, come le firme timbrate
    var r = canvas.getBoundingClientRect(), largo = 600, alto = Math.round(r.height * largo / r.width);
    var c = document.createElement('canvas'); c.width = largo; c.height = alto;
    var x = c.getContext('2d');
    if (!timbroImg.hidden && timbroImg.naturalWidth) {
      var m = 0.08 * alto, s = Math.min((largo - 2 * m) / timbroImg.naturalWidth, (alto - 2 * m) / timbroImg.naturalHeight);
      var w = timbroImg.naturalWidth * s, h = timbroImg.naturalHeight * s;
      x.globalAlpha = 0.9; x.drawImage(timbroImg, (largo - w) / 2, (alto - h) / 2, w, h); x.globalAlpha = 1;
    }
    x.drawImage(canvas, 0, 0, largo, alto);
    attuale.value = c.toDataURL('image/png');
    var form = attuale.form, bottone = invio;
    chiudi();
    if (form.requestSubmit) form.requestSubmit(bottone && bottone.form === form ? bottone : undefined); else form.submit();
  });

  campi.forEach(function (campo) {
    var form = campo.form; if (!form) return;
    form.addEventListener('submit', function (e) {
      if (campo.value || campo.dataset.saltata) return;
      if (e.submitter && e.submitter.dataset.senzaFirma !== undefined) return;  // es. "Predisponi": nessuna partenza
      e.preventDefault(); e.stopImmediatePropagation();
      delete form.dataset.firmaAnnullata;
      apri(campo, e.submitter);
    }, true);
  });
})();
