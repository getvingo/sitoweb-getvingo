// Nuova auto a passi (22/11/2026, templates/abbina.html): VIN → Dati → Nuova/Usata.
// La lettura del VIN la fa vin.js (eventi "vin-fase" e "vin-precompilato"); qui si decide cosa si vede.
// I campi sono quelli di sempre (stessi nomi, stessi controlli del server): i mancanti stanno in
// "Da completare" con scelte a pulsanti, i trovati come righe da toccare per correggerli uno alla volta.
(function () {
  var form = document.getElementById('na');
  if (!form) return;
  var $ = function (sel, el) { return (el || form).querySelector(sel); };
  var $$ = function (sel, el) { return Array.prototype.slice.call((el || form).querySelectorAll(sel)); };
  var campoVin = $('#vin');
  var VALIDO = /^[A-HJ-NPR-Z0-9]{17}$/;
  var OBBLIGATORI = ['tag', 'marca', 'modello', 'versione', 'anno', 'colore', 'alimentazione', 'sede_id'];
  var COLORI_BASE = ['Bianco', 'Nero', 'Grigio', 'Blu', 'Rosso', 'Argento'];
  var dati = {}, daFoto = false, distribuiti = false, chiaveVersioni = '';

  // ---------------------------------------------------------------- passi
  function vai(passo) {
    form.dataset.passo = passo;
    var n = passo === 'dati' ? (mancanti().length ? 1 : 2) : 0;
    $$('.na-passi span').forEach(function (s) {
      var k = +s.dataset.p;
      s.className = k < n ? 'fatto' : (k === n ? 'ora' : '');
    });
    if (passo === 'dati') entraDati();
    if (passo === 'mano') setTimeout(function () { campoVin.focus(); }, 50);
    window.scrollTo(0, 0);
  }
  $$('[data-vai]').forEach(function (b) { b.addEventListener('click', function () { vai(b.dataset.vai); }); });

  // ---------------------------------------------------------------- lettura dalla foto
  var TAPPE = ['barre', 'caratteri', 'controllo'];
  function tappa(nome) {
    var k = TAPPE.indexOf(nome);
    $$('[data-tappa]').forEach(function (li, j) {
      li.className = j < k ? 'fatta' : (j === k ? 'ora' : '');
      li.querySelector('.p').innerHTML = j < k ? '<svg class="icona" aria-hidden="true"><use href="#i-check"/></svg>' : '';
    });
  }
  document.addEventListener('vin-fase', function (e) {
    var d = e.detail;
    if (d.fase === 'foto') {
      daFoto = true;
      $$('[data-foto]').forEach(function (img) { img.src = d.url; });
      tappa('barre'); vai('lettura');
    }
    if (d.fase === 'caratteri' || d.fase === 'controllo') tappa(d.fase);
    if (d.fase === 'letto') tappa('fine');
    if (d.fase === 'errore') vai('errore');
  });

  // dati dal server: auto già in stock → doppione; letto dalla foto → "È giusto?"; scritto a mano → dati
  document.addEventListener('vin-precompilato', function (e) {
    dati = e.detail || {};
    var cerca = $('[data-cerca]'); if (cerca) cerca.hidden = true;
    if (dati.esistente) return doppione(dati.esistente);
    if (daFoto) { daFoto = false; conferma(); return; }
    vai('dati');
  });
  campoVin.addEventListener('input', function () {
    var cerca = $('[data-cerca]'); if (cerca) cerca.hidden = campoVin.value.length !== 17;
  });

  function gruppi(v) {
    return '<span>' + v.slice(0, 3) + '<small>costruttore</small></span><span>' + v.slice(3, 9) +
      '<small>modello</small></span><span><b>' + v.slice(9) + '</b><small>numero di serie</small></span>';
  }
  function pillola(classe, icona, testo) {
    return '<span class="na-pillola ' + classe + '"><svg class="icona s" aria-hidden="true"><use href="#i-' + icona + '"/></svg>' + testo + '</span>';
  }
  function avvisoVin(box) {
    box.innerHTML = '';
    if (!dati.avvisi || !dati.avvisi.length) return;
    var p = document.createElement('p'); p.className = 'avviso attenzione';
    p.innerHTML = '<svg class="icona" aria-hidden="true"><use href="#i-attenzione"/></svg><span></span>';
    p.querySelector('span').textContent = dati.avvisi[0] + ' ';
    if (dati.proposta) {
      var usa = document.createElement('button');
      usa.type = 'button'; usa.className = 'btn-link'; usa.textContent = 'Usa ' + dati.proposta;
      usa.addEventListener('click', function () {
        campoVin.value = dati.proposta; daFoto = true;
        campoVin.dispatchEvent(new Event('input', { bubbles: true }));
      });
      p.querySelector('span').appendChild(usa);
    }
    box.appendChild(p);
  }
  function conferma() {
    var v = campoVin.value.toUpperCase();
    $('[data-vin-gruppi]').innerHTML = gruppi(v);
    var ok = !(dati.avvisi && dati.avvisi.length);
    $('[data-controlli]').innerHTML = pillola('ok', 'check', '17 caratteri') +
      pillola('ok', 'check', 'Mai abbinato a un adesivo') +
      (ok ? '' : pillola('att', 'attenzione', 'Cifra di controllo'));
    avvisoVin($('[data-avviso-vin]'));
    vai('conferma');
  }

  // ---------------------------------------------------------------- auto già in Getvingo
  function cartaAuto(nome, marca, logo, vin) {
    var l = logo ? '<img src="' + logo + '" alt="">' : (marca || '?').slice(0, 2).toUpperCase();
    return '<span class="na-logo">' + l + '</span><span><b></b><small>' + vin.slice(0, 9) + '<b>' + vin.slice(9) + '</b></small></span>';
  }
  function doppione(es) {
    var box = $('[data-doppione-auto]');
    box.innerHTML = cartaAuto(es.nome, es.marca, es.logo, campoVin.value.toUpperCase());
    box.querySelector('span b').textContent = es.nome;
    $('[data-doppione-info]').textContent = 'Ha già l\'adesivo ' + es.adesivo + ' · ' + es.fase + (es.dove ? ' · ' + es.dove : '') + '.';
    $$('[data-se]').forEach(function (b) { b.hidden = !es[b.dataset.se]; });
    var ddt = $('[data-ddt-aperto]');
    if (ddt) {
      ddt.hidden = !es.ddt;
      if (es.ddt) { ddt.href = es.ddt.url; ddt.querySelector('span').textContent = 'Apri il DDT ' + es.ddt.sigla; }
    }
    var apri = $('[data-apri-auto]'); if (apri) apri.href = '/veicolo/' + es.id;
    daFoto = false;
    vai('doppione');
  }

  // ---------------------------------------------------------------- dati
  function el(c) { return form.elements[c]; }
  function valore(c) {
    if (c === 'tipologia') { var r = $('[name=tipologia]:checked'); return r ? r.value : ''; }
    var x = el(c); return x ? (x.value || '').trim() : '';
  }
  function testo(c) {
    var v = valore(c);
    if (!v) return '';
    if (c === 'tipologia') return v === 'commerciale' ? 'Veicolo commerciale' : 'Vettura';
    var x = el(c);
    if (x && x.tagName === 'SELECT') { var o = x.options[x.selectedIndex]; return o ? o.text.split(' · ')[0] : v; }
    return v;
  }
  function blocco(c) { return $('.na-campo[data-campo="' + c + '"]'); }
  function mancanti() {
    return OBBLIGATORI.filter(function (c) { return blocco(c) && !valore(c); });
  }
  function fonte(c) {
    if (c === 'sede_id') return form.dataset.sedePosizione && !blocco(c).dataset.toccato ? 'POSIZIONE' : '';
    return (dati.fonti || {})[c] && !blocco(c).dataset.toccato ? dati.fonti[c] : '';
  }
  function imposta(c, v) {
    if (c === 'tipologia') { var r = $('[name=tipologia][value="' + v + '"]'); if (r) r.checked = true; }
    else el(c).value = v;
    blocco(c).dataset.toccato = '1';
    chiudi(c);
    if (c === 'marca' || c === 'modello' || c === 'anno') caricaVersioni();
    aggiorna();
  }
  function rendi(c) {
    var b = blocco(c); if (!b) return;
    b.querySelector('[data-val]').textContent = testo(c) || '—';
    var f = b.querySelector('[data-fonte]'); if (f) { f.textContent = fonte(c); f.hidden = !fonte(c); }
    var v = valore(c);
    b.querySelectorAll('[data-scelte] [data-v]').forEach(function (x) { x.setAttribute('aria-pressed', String(x.dataset.v === v)); });
  }
  function apri(c) {
    var b = blocco(c); b.classList.add('aperto');
    var libero = b.querySelector('.na-libero');
    var scelte = b.querySelector('[data-scelte]');
    // senza pulsanti (o con un valore che non c'è tra i pulsanti) si scrive/sceglie nel campo
    if (libero && (!scelte || !scelte.querySelector('[data-v]') || (valore(c) && !scelte.querySelector('[data-v="' + cssEsc(valore(c)) + '"]')))) libero.hidden = false;
  }
  function chiudi(c) {
    var b = blocco(c); b.classList.remove('aperto');
  }
  function cssEsc(s) { return String(s).replace(/["\\]/g, '\\$&'); }
  function aggiorna() {
    OBBLIGATORI.concat(['tipologia']).forEach(rendi);
    var m = mancanti();
    var t = $('[data-manca-t]');
    if (t && !t.hidden) { t.textContent = m.length ? 'Da completare · ' + m.length : 'Completato'; t.classList.toggle('arancio', !!m.length); }
    var ancora = $('[data-manca-ancora]');
    ancora.hidden = !m.length;
    ancora.querySelector('span').textContent = 'Manca: ' + m.map(function (c) { return blocco(c).dataset.nome.toLowerCase(); }).join(', ');
    $$('.na-nu button').forEach(function (b) { b.classList.toggle('spento', !!m.length); b.setAttribute('aria-disabled', String(!!m.length)); });
    if (form.dataset.passo === 'dati') $$('.na-passi span').forEach(function (s) { var k = +s.dataset.p; s.className = k < (m.length ? 1 : 2) ? 'fatto' : (k === (m.length ? 1 : 2) ? 'ora' : ''); });
    // carta dell'auto in alto
    var v = campoVin.value.toUpperCase();
    if (VALIDO.test(v)) {
      var box = $('[data-auto]');
      box.innerHTML = cartaAuto('', valore('marca'), dati.logo, v);
      box.querySelector('span b').textContent = [valore('marca'), valore('modello')].join(' ').trim() || 'Nuova auto';
    }
  }

  function entraDati() {
    avvisoVin($('[data-avviso-dati]'));
    if (!distribuiti) {
      distribuiti = true;
      var manca = $('[data-manca]'), trovati = $('[data-trovati]');
      $$('.na-campo').forEach(function (b) {
        var c = b.dataset.campo;
        if (OBBLIGATORI.indexOf(c) >= 0 && !valore(c)) { manca.appendChild(b); apri(c); }
        else trovati.appendChild(b);
      });
      $('[data-manca-t]').hidden = !manca.children.length;
      $('[data-trovati-t]').hidden = !trovati.children.length;
      caricaVersioni();
    }
    aggiorna();
  }

  // un tocco su una riga trovata apre solo quel dato; sui pulsanti si sceglie
  form.addEventListener('click', function (e) {
    var riga = e.target.closest('[data-apri]');
    if (riga) { var c = riga.closest('.na-campo').dataset.campo; apri(c); var x = blocco(c).querySelector('.na-libero:not([hidden])'); if (x && x.tagName === 'INPUT') x.focus(); return; }
    var scelta = e.target.closest('[data-scelte] [data-v]');
    if (scelta) { imposta(scelta.closest('.na-campo').dataset.campo, scelta.dataset.v); return; }
    var altro = e.target.closest('[data-altro]');
    if (altro) {
      var b = altro.closest('.na-campo'), l = b.querySelector('.na-libero');
      l.hidden = false; l.focus(); return;
    }
  });
  // campi scritti: valgono all'uscita (o con Invio)
  $$('.na-libero').forEach(function (x) {
    var c = x.closest('.na-campo').dataset.campo;
    x.addEventListener('change', function () { if (x.value.trim()) { blocco(c).dataset.toccato = '1'; if (x.tagName === 'SELECT') chiudi(c); if (c === 'marca' || c === 'modello' || c === 'anno') caricaVersioni(); aggiorna(); } });
    x.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); x.blur(); if (x.value.trim()) chiudi(c); aggiorna(); } });
    x.addEventListener('input', aggiorna);
  });

  // versioni già usate (e del catalogo) e colori più usati della marca, a pulsanti
  function pulsanti(box, voci, altro) {
    box.innerHTML = '';
    voci.forEach(function (v) {
      var b = document.createElement('button'); b.type = 'button'; b.dataset.v = v; b.textContent = v; box.appendChild(b);
    });
    if (altro) { var a = document.createElement('button'); a.type = 'button'; a.className = 'altro'; a.dataset.altro = '1';
      a.innerHTML = '<svg class="icona s" aria-hidden="true"><use href="#i-piu"/></svg> ' + altro; box.appendChild(a); }
  }
  function caricaVersioni() {
    var marca = valore('marca'), modello = valore('modello'), anno = valore('anno');
    var chiave = marca + '|' + modello + '|' + anno;
    if (chiave === chiaveVersioni) return;
    chiaveVersioni = chiave;
    var bv = blocco('versione'), bc = blocco('colore');
    var fine = function (d) {
      var versioni = (d.catalogo || []).concat(d.anno || [], d.altre || []).slice(0, 6);
      pulsanti(bv.querySelector('[data-scelte]'), versioni, versioni.length ? 'Altra…' : '');
      bv.querySelector('.na-libero').hidden = !!versioni.length && !(valore('versione') && versioni.indexOf(valore('versione')) < 0);
      bv.querySelector('[data-aiuto]').textContent = versioni.length ? '' : (marca && modello ? 'prima ' + marca + ' ' + modello + ' in Getvingo: scrivila' : '');
      var colori = (d.colori || []).slice(0, 6);
      COLORI_BASE.forEach(function (c) { if (colori.length < 6 && colori.indexOf(c) < 0) colori.push(c); });
      pulsanti(bc.querySelector('[data-scelte]'), colori, 'Altro…');
      bc.querySelector('.na-libero').hidden = !!valore('colore') ? colori.indexOf(valore('colore')) >= 0 : true;
      aggiorna();
    };
    if (!marca || !modello) return fine({});
    fetch('/api/versioni?' + new URLSearchParams({ marca: marca, modello: modello, anno: anno }))
      .then(function (r) { return r.ok ? r.json() : {}; }).then(fine).catch(function () { fine({}); });
  }

  // targa facoltativa
  var targaApri = $('[data-targa-apri]');
  targaApri.addEventListener('click', function () { targaApri.hidden = true; $('[data-targa]').hidden = false; $('[name=targa]').focus(); });

  // Nuova / Usata: finché manca qualcosa non si salva, si porta al primo dato mancante
  function vaiAlMancante() {
    var m = mancanti(); if (!m.length) return;
    var b = blocco(m[0]); apri(m[0]);
    b.scrollIntoView({ behavior: 'smooth', block: 'center' });
    b.classList.remove('lampeggia'); void b.offsetWidth; b.classList.add('lampeggia');
  }
  $('[data-manca-ancora]').addEventListener('click', vaiAlMancante);
  form.addEventListener('submit', function (e) {
    var chi = e.submitter;
    if (chi && chi.getAttribute('formaction')) return;  // auto già in Getvingo: va dove ha scelto
    // 23/11/2026: usata o importata senza km → si chiedono i km prima di salvare
    var km = $('[name=km]');
    if (chi && (chi.value === 'usato' || chi.value === 'importato') && km && !/\d/.test(km.value) && !mancanti().length) {
      e.preventDefault(); e.stopImmediatePropagation();
      setTimeout(function () { delete form.dataset.inviato; });
      var b = $('[data-km]'); b.classList.add('da-scrivere');
      b.querySelector('span').innerHTML = 'Scrivi i km dell\'auto ' + (chi.value === 'usato' ? 'usata' : 'importata') + ', poi premi di nuovo';
      b.scrollIntoView({ behavior: 'smooth', block: 'center' }); km.focus();
      b.classList.remove('lampeggia'); void b.offsetWidth; b.classList.add('lampeggia');
      return;
    }
    if (mancanti().length || !VALIDO.test(campoVin.value.toUpperCase())) {
      e.preventDefault(); e.stopImmediatePropagation();
      setTimeout(function () { delete form.dataset.inviato; });  // "una volta sola": l'invio vero non c'è stato
      if (!VALIDO.test(campoVin.value.toUpperCase())) vai('mano'); else vaiAlMancante();
    }
  }, true);

  // ritorno dal server con un errore (o VIN già scritto): si riparte dai dati
  if (form.dataset.haVin && campoVin.value) {
    campoVin.dispatchEvent(new Event('input', { bubbles: true }));  // vin.js rilegge i dati → passo Dati
    vai('dati');
  } else vai('vin');
})();
