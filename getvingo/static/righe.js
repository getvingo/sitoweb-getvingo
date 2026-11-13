// Elenchi a righe (Lavorazioni, Preparazione; 13/11/2026): riga cliccabile, ricerca che filtra mentre si
// scrive, riga dei dettagli che si apre con "⋯" (o con l'azione che la richiede, es. Sollecita)
(function () {
  document.querySelectorAll('.riga-stock[data-url]').forEach(function (r) {
    r.addEventListener('click', function (e) {
      if (!e.target.closest('a, button, summary, form, input, select, textarea, label')) location.href = r.dataset.url;
    });
  });
  document.querySelectorAll('[data-apri-riga]').forEach(function (b) {
    b.addEventListener('click', function () {
      var riga = document.getElementById(b.dataset.apriRiga);
      if (!riga) return;
      var apri = riga.hidden || !b.classList.contains('btn-icona');
      riga.hidden = !apri;
      document.querySelectorAll('[data-apri-riga="' + b.dataset.apriRiga + '"].btn-icona').forEach(function (x) {
        x.setAttribute('aria-expanded', String(apri)); x.classList.toggle('attivo', apri);
      });
      if (apri && !b.classList.contains('btn-icona')) {  // "Sollecita": si apre direttamente il modulo
        var d = riga.querySelector('details.sollecita'); if (d) { d.open = true; var t = d.querySelector('textarea'); if (t) t.focus(); }
      }
    });
  });
  var campo = document.getElementById('cerca-righe'), nessuna = document.getElementById('righe-nessuna');
  if (!campo) return;
  campo.addEventListener('input', function () {
    var parole = campo.value.toLowerCase().split(/\s+/).filter(Boolean), totale = 0;
    document.querySelectorAll('tbody.gruppo-stock').forEach(function (g) {
      var visibili = 0;
      g.querySelectorAll('.riga-stock').forEach(function (r) {
        var ok = parole.every(function (p) { return (r.dataset.cerca || '').indexOf(p) >= 0; });
        r.hidden = !ok; if (ok) visibili++;
        var d = r.nextElementSibling;
        if (!ok && d && d.classList.contains('riga-dettagli')) d.hidden = true;
      });
      g.hidden = !visibili; totale += visibili;
    });
    if (nessuna) nessuna.hidden = totale > 0;
  });
  campo.form && campo.form.addEventListener('submit', function (e) { if (document.activeElement === campo) e.preventDefault(); });
})();
