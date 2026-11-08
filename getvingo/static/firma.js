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
