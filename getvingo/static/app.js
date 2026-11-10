(function () {
  // Posizione: si chiede solo al tocco NFC (tocco.html) e alla conferma d'arrivo. Gli altri moduli
  // non la chiedono: il server usa quella rilevata all'ultimo tocco (routes._coord).
  var demo = document.body.hasAttribute('data-demo');

  // Dati fiscali che seguono il tipo di luogo: sede della concessionaria = niente; cliente = P. IVA facoltativa
  document.querySelectorAll('[data-fiscali-per-tipo]').forEach(function (box) {
    var form = box.closest('form');
    var aggiorna = function () {
      var t = form.querySelector('[name=tipo]:checked, select[name=tipo]');
      var tipo = t ? t.value : '';
      box.hidden = tipo === 'propria';
      box.querySelector('[data-piva-facoltativa]').hidden = tipo !== 'cliente';
    };
    form.querySelectorAll('[name=tipo]').forEach(function (el) { el.addEventListener('change', aggiorna); });
    aggiorna();
  });

  // Marca e modello: "volkswagen golf" → "Volkswagen", "Golf". Si propone una volta; se poi lo si corregge
  // (es. BMW, e-tron) resta come scritto
  document.querySelectorAll('input[data-formato=nome]').forEach(function (el) {
    var formatta = function (t) {
      return t.trim().replace(/\s+/g, ' ').toLowerCase().replace(/(^|[\s\-\/])(\S)/g, function (m, p, c) { return p + c.toUpperCase(); });
    };
    el.addEventListener('blur', function () {
      if (el.dataset.corretto || !el.value.trim()) return;
      var f = formatta(el.value);
      if (f !== el.value) { el.value = f; el.dataset.formattato = f; }
    });
    el.addEventListener('input', function () {
      if (el.dataset.formattato && el.value !== el.dataset.formattato) el.dataset.corretto = '1';
    });
  });

  // Conferma tattile dopo un'azione riuscita
  if (document.querySelector('[data-vibra=ok]') && navigator.vibrate) navigator.vibrate(60);

  // Causale suggerita dal tipo di destinazione (solo finché l'utente non la cambia a mano)
  var dest = document.getElementById('destinazione');
  var causale = document.getElementById('causale');
  var riepilogo = document.querySelector('[data-riepilogo]');
  var toccata = false;
  var SUGGERITE = { propria: 'Spostamento', carrozzeria: 'Invio lavorazione', officina: 'Invio lavorazione',
                    altro: 'Invio lavorazione', allestitore: 'Invio lavorazione', esposizione: 'Conto esposizione',
                    concessionario: 'Conto visione', rivenditore: 'Conto visione', cliente: 'Consegna a cliente' };
  if (causale) causale.addEventListener('change', function () { toccata = true; });
  function aggiorna(suggerisci) {
    if (!dest) return;
    var opt = dest.options[dest.selectedIndex];
    var tipo = opt && opt.dataset.tipo;
    if (suggerisci === true && !toccata && causale && SUGGERITE[tipo]) causale.value = SUGGERITE[tipo];
    if (riepilogo) {
      riepilogo.textContent = opt && opt.value ? 'verso ' + opt.text : 'scegli prima la destinazione';
    }
  }
  if (dest) { dest.addEventListener('change', function () { aggiorna(true); }); aggiorna(false); }

  // dettagli della lavorazione: obbligatori, tranne per il solo trasferimento
  var lav = document.getElementById('lavorazione'), note = document.getElementById('lavorazione-note');
  function obbligoNote() {
    var nessuna = lav.value === 'Nessuna (solo trasferimento)';
    note.required = !nessuna;
    document.querySelectorAll('[data-se-nessuna]').forEach(function (s) { s.textContent = nessuna ? '(non serve per il solo trasferimento)' : '(obbligatori)'; });
  }
  if (lav && note) { lav.addEventListener('change', obbligoNote); obbligoNote(); }

  // Panoramica: elementi in entrata uno dopo l'altro e numeri che contano fino al valore
  var panoramica = document.querySelector('.panoramica');
  if (panoramica) {
    ['.riquadri a', '.oggi-voce', '.titolo-gruppo, .da-fare .voce'].forEach(function (sel) {
      panoramica.querySelectorAll(sel).forEach(function (el, n) {
        el.style.setProperty('--ritardo', Math.min(n * 0.06, 0.9) + 's');
        var barra = el.querySelector('.barra-stato');
        if (barra) barra.style.setProperty('--ritardo', (0.2 + n * 0.06) + 's');
      });
    });
    if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      panoramica.querySelectorAll('[data-conta]').forEach(function (el) {
        var fine = parseInt(el.dataset.conta, 10), inizio = null, durata = 700;
        if (!fine) return;
        el.textContent = '0';
        function passo(t) {
          if (inizio === null) inizio = t;
          var k = Math.min((t - inizio) / durata, 1);
          el.textContent = Math.round(fine * (1 - Math.pow(1 - k, 3)));
          if (k < 1) requestAnimationFrame(passo);
        }
        requestAnimationFrame(passo);
      });
    }
  }

  // Riquadri apribili (es. Preparazione): un tocco sullo sfondo apre la scheda; pulsanti, campi e link restano i loro
  document.querySelectorAll('[data-apri-url]').forEach(function (r) {
    r.addEventListener('click', function (e) {
      if (e.target.closest('a, button, input, select, textarea, label, summary, details, form')) return;
      if (window.getSelection && String(window.getSelection())) return;  // sta selezionando del testo
      location.href = r.dataset.apriUrl;
    });
  });

  // Mappe bloccate: si zooma con + e −; per trascinare o zoomare con rotella/dita si tocca prima la mappa.
  // Toccando fuori dalla mappa torna bloccata (la pagina scorre senza spostarla per sbaglio).
  window.bloccaMappa = function (mappa) {
    var el = mappa.getContainer(), comandi = ['dragging', 'touchZoom', 'doubleClickZoom', 'scrollWheelZoom', 'boxZoom', 'keyboard'];
    function imposta(attiva) {
      comandi.forEach(function (c) { if (mappa[c]) mappa[c][attiva ? 'enable' : 'disable'](); });
      el.classList.toggle('mappa-bloccata', !attiva);
    }
    imposta(false);
    mappa.on('click', function () { if (el.classList.contains('mappa-bloccata')) imposta(true); });
    document.addEventListener('pointerdown', function (e) { if (!el.contains(e.target)) imposta(false); });
  };

  // "Arrivato a destinazione": prima di inviare serve una posizione fresca del telefono
  // (il server chiude il DDT solo entro il raggio della destinazione)
  document.querySelectorAll('form[data-gps-obbligatorio]').forEach(function (f) {
    var errore = f.querySelector('[data-gps-errore]');
    var MOTIVI = { 1: 'Hai negato l’accesso alla posizione: senza non si può chiudere il DDT.',
                   2: 'Posizione non disponibile: attiva il GPS / la localizzazione del telefono.',
                   3: 'Il GPS non ha risposto in tempo: riprova.' };
    f.addEventListener('submit', function (e) {
      if (f.dataset.posizione || !navigator.geolocation || demo) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      var bottone = e.submitter;
      if (bottone) bottone.classList.add('in-corso');
      if (errore) errore.hidden = true;
      navigator.geolocation.getCurrentPosition(function (p) {
        f.lat.value = p.coords.latitude; f.lon.value = p.coords.longitude;
        f.dataset.posizione = '1';
        if (f.requestSubmit) f.requestSubmit(bottone); else f.submit();
      }, function (err) {
        if (f.hasAttribute('data-senza-gps-ok')) {  // il fornitore destinatario conferma anche da lontano
          f.dataset.posizione = '1';
          if (f.requestSubmit) f.requestSubmit(bottone); else f.submit();
          return;
        }
        if (bottone) bottone.classList.remove('in-corso');
        if (errore) { errore.textContent = MOTIVI[err.code] || MOTIVI[2]; errore.hidden = false; }
      }, { enableHighAccuracy: true, timeout: 20000, maximumAge: 0 });
    });
  });

  // Condividi il PDF del DDT: sul telefono si condivide il file (scaricato prima, perché la
  // condivisione deve partire subito dal tocco); altrimenti il link, oppure lo si copia
  document.querySelectorAll('[data-condividi-pdf]').forEach(function (b) {
    var url = new URL(b.dataset.condividiPdf, location.href).href, titolo = b.dataset.titolo, file = null;
    if (navigator.canShare && window.File) {
      fetch(url, { credentials: 'same-origin' }).then(function (r) { return r.ok ? r.blob() : null; }).then(function (blob) {
        var f = blob && new File([blob], b.dataset.nome, { type: 'application/pdf' });
        if (f && navigator.canShare({ files: [f] })) file = f;
      }).catch(function () {});
    }
    b.addEventListener('click', function () {
      if (file) { navigator.share({ files: [file], title: titolo }).catch(function () {}); return; }
      if (navigator.share) { navigator.share({ title: titolo, url: url }).catch(function () {}); return; }
      if (navigator.clipboard) navigator.clipboard.writeText(url).then(function () {
        var prima = b.innerHTML; b.textContent = 'Link copiato';
        setTimeout(function () { b.innerHTML = prima; }, 1800);
      });
    });
  });

  // Scarica PDF dall'app installata sul telefono (schermo intero, senza barra del browser): aprire il file
  // porterebbe in un visore senza via d'uscita. Lì si scarica in memoria e si apre la scheda di sistema
  // (Salva su File / Condividi); nel browser normale resta il download classico.
  var installata = (window.matchMedia && matchMedia('(display-mode: standalone)').matches) || navigator.standalone === true;
  document.querySelectorAll('[data-scarica-pdf]').forEach(function (a) {
    if (!installata || !(navigator.canShare && window.File)) return;
    var file = null;
    fetch(a.href, { credentials: 'same-origin' }).then(function (r) { return r.ok ? r.blob() : null; }).then(function (blob) {
      var f = blob && new File([blob], a.dataset.nome, { type: 'application/pdf' });
      if (f && navigator.canShare({ files: [f] })) file = f;
    }).catch(function () {});
    a.addEventListener('click', function (e) {
      e.preventDefault();
      if (file) { navigator.share({ files: [file] }).catch(function () {}); return; }
      var prima = a.innerHTML; a.textContent = 'Preparo il PDF…';
      setTimeout(function () { a.innerHTML = prima; }, 1500);
    });
  });
  // DDT appena chiuso: nel browser il PDF firmato si scarica da solo (nell'app installata resta il pulsante,
  // perché lì serve un tocco per aprire Salva su File)
  var subito = document.querySelector('[data-scarica-subito]');
  if (subito && !installata) {
    var copia = document.createElement('a');
    copia.href = subito.href; copia.download = subito.dataset.nome; copia.hidden = true;
    document.body.appendChild(copia); copia.click(); copia.remove();
  }
  // Esc chiude le pagine-riquadro (anteprima del DDT) come i pannelli
  var chiudiEsc = document.querySelector('[data-chiudi-esc]');
  if (chiudiEsc) document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !/INPUT|TEXTAREA|SELECT/.test((document.activeElement || {}).tagName)) chiudiEsc.click();
  });

  // "In viaggio da …": parte dai secondi calcolati dal server e avanza da solo
  var contatori = document.querySelectorAll('[data-in-viaggio]');
  if (contatori.length) {
    var caricata = Date.now();
    var scrivi = function () {
      contatori.forEach(function (el) {
        var m = Math.floor((parseInt(el.dataset.inViaggio, 10) + (Date.now() - caricata) / 1000) / 60);
        el.textContent = m < 1 ? 'meno di un minuto' : m < 60 ? m + ' min'
          : m < 1440 ? Math.floor(m / 60) + ' h' + (m % 60 ? ' ' + (m % 60) + ' min' : '')
          : Math.floor(m / 1440) + ' g' + (Math.floor(m % 1440 / 60) ? ' ' + Math.floor(m % 1440 / 60) + ' h' : '');
      });
    };
    scrivi();
    setInterval(scrivi, 20000);
  }

  // Anello dell'avanzamento della preparazione: si riempie dal valore di prima a quello attuale
  document.querySelectorAll('[data-anello]').forEach(function (el) {
    var cerchio = el.querySelector('.anello-pieno'), testo = el.querySelector('[data-percento]');
    var da = parseInt(el.dataset.da, 10) || 0, a = parseInt(el.dataset.a, 10) || 0, giro = 326.73;
    var fermo = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    cerchio.style.transition = 'none';
    cerchio.style.strokeDashoffset = giro * (1 - da / 100);
    cerchio.getBoundingClientRect();  // applica subito il punto di partenza
    cerchio.style.transition = '';
    requestAnimationFrame(function () { cerchio.style.strokeDashoffset = giro * (1 - a / 100); });
    if (fermo || da === a) { testo.textContent = a + '%'; return; }
    var inizio = null;
    (function conta(t) {
      if (inizio === null) inizio = t;
      var k = Math.min((t - inizio) / 1100, 1);
      testo.textContent = Math.round(da + (a - da) * (1 - Math.pow(1 - k, 3))) + '%';
      if (k < 1) requestAnimationFrame(conta);
    })(performance.now());
    if (a === 100 && navigator.vibrate) navigator.vibrate([60, 60, 120]);
  });

  // Barre di avanzamento (lavorazioni esterne): partono vuote e si riempiono
  if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    var barre = document.querySelectorAll('[data-riempi]');
    barre.forEach(function (b) { b.style.width = '0%'; });
    requestAnimationFrame(function () { requestAnimationFrame(function () {
      barre.forEach(function (b) { b.style.width = b.dataset.riempi + '%'; });
    }); });
  }

  // Evita doppio invio (doppio DDT) se si tocca due volte il bottone
  // (non si disabilita il bottone: il suo name/value, es. "predisponi", deve arrivare al server)
  document.querySelectorAll('form[data-una-volta]').forEach(function (f) {
    f.addEventListener('submit', function (e) {
      if (f.dataset.inviato) { e.preventDefault(); return; }
      f.dataset.inviato = '1';
      if (e.submitter) e.submitter.classList.add('in-corso');
    });
  });

  // Filtri ripiegati solo su telefono: da schermo largo restano sempre aperti
  if (window.matchMedia('(min-width: 560px)').matches) {
    document.querySelectorAll('details.filtri-ripiegabili').forEach(function (d) { d.open = true; });
  }

  // Ogni riquadro apribile ha il suo "✕ Chiudi" in alto a destra; Esc chiude l'ultimo aperto
  var APRIBILI = 'details.ripiega, details.sezione-azione, details.apri-con-pulsante, details.dettagli-auto, details.filtri-ripiegabili';
  document.querySelectorAll(APRIBILI).forEach(function (d) {
    var b = document.createElement('button');
    b.type = 'button'; b.className = 'chiudi-pannello'; b.setAttribute('aria-label', 'Chiudi');
    b.innerHTML = '<svg class="icona s" aria-hidden="true"><use href="#i-freccia-su"/></svg> Chiudi';
    b.addEventListener('click', function () {
      d.open = false;
      var s = d.querySelector('summary'); if (s) s.focus({ preventScroll: true });
    });
    d.insertBefore(b, d.querySelector('summary').nextSibling);
  });
  // pannelli aperti da un pulsante in alto (es. "Metti in preparazione"): si chiudono con la freccia
  document.querySelectorAll('[data-apri]').forEach(function (b) {
    var pannello = document.getElementById(b.dataset.apri);
    b.addEventListener('click', function () {
      pannello.hidden = !pannello.hidden;
      b.setAttribute('aria-expanded', String(!pannello.hidden));
      if (!pannello.hidden) pannello.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    });
  });
  document.querySelectorAll('[data-chiudi]').forEach(function (b) {
    b.addEventListener('click', function () {
      document.getElementById(b.dataset.chiudi).hidden = true;
      var apri = document.querySelector('[data-apri="' + b.dataset.chiudi + '"]');
      if (apri) { apri.setAttribute('aria-expanded', 'false'); apri.focus(); }
    });
  });
  // "Indietro": se si arriva da un'altra pagina del portale si torna lì, altrimenti si segue il link
  document.querySelectorAll('[data-indietro]').forEach(function (a) {
    a.addEventListener('click', function (e) {
      if (document.referrer.indexOf(location.origin + '/') === 0 && history.length > 1) { e.preventDefault(); history.back(); }
    });
  });
  var menu = document.querySelector('details.menu-utente');
  document.addEventListener('click', function (e) {  // il menu utente si chiude toccando fuori
    if (menu && menu.open && !menu.contains(e.target)) menu.open = false;
  });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    if (menu && menu.open) { menu.open = false; return; }
    var aperti = [].filter.call(document.querySelectorAll(APRIBILI), function (d) { return d.open && d.querySelector('.chiudi-pannello'); });
    if (aperti.length) aperti[aperti.length - 1].open = false;
  });

  // Scelta dell'orario a slot: giorno -> orario libero. Gli slot presi (per la sede scelta) non si toccano.
  var GIORNI = ['dom', 'lun', 'mar', 'mer', 'gio', 'ven', 'sab'];
  document.querySelectorAll('[data-scegli-slot]').forEach(function (box) {
    var campo = box.querySelector('input[type=hidden]'), testo = box.querySelector('[data-slot-testo]');
    var togli = box.querySelector('[data-slot-togli]'), form = box.closest('form');
    var sede = box.dataset.perSede && form ? form.querySelector('[name="' + box.dataset.perSede + '"]') : null;
    var giorni = [].slice.call(box.querySelectorAll('.slot-giorno')), pannelli = [].slice.call(box.querySelectorAll('.slot-ore'));
    function occupatoDa(b) {
      var o = JSON.parse(b.dataset.occupato || 'null');
      if (o && typeof o === 'object') o = sede ? o[sede.value] : null;
      return o || '';
    }
    function etichetta(at) {
      var d = new Date(at), fine = new Date(d.getTime() + 30 * 60000);
      var due = function (n) { return (n < 10 ? '0' : '') + n; };
      return GIORNI[d.getDay()] + ' ' + due(d.getDate()) + '/' + due(d.getMonth() + 1) + ' · ' +
             due(d.getHours()) + ':' + due(d.getMinutes()) + '–' + due(fine.getHours()) + ':' + due(fine.getMinutes());
    }
    function aggiorna() {
      pannelli.forEach(function (p, n) {
        var liberi = 0;
        p.querySelectorAll('.slot-ora').forEach(function (b) {
          var chi = occupatoDa(b), scelto = b.dataset.at === campo.value;
          if (chi && scelto) { campo.value = ''; scelto = false; }
          b.disabled = !!chi;
          b.classList.toggle('occupato', !!chi);
          b.classList.toggle('scelto', scelto);
          b.querySelector('small').textContent = chi;
          b.title = chi ? 'Già preso: ' + chi : '';
          if (!chi) liberi++;
        });
        giorni[n].querySelector('.slot-liberi').textContent = liberi ? liberi + ' liberi' : 'pieno';
        giorni[n].classList.toggle('pieno', !liberi);
        giorni[n].classList.toggle('con-scelta', !!p.querySelector('.slot-ora.scelto'));
      });
      testo.textContent = campo.value ? etichetta(campo.value) : box.dataset.vuoto;
      togli.hidden = !campo.value;
    }
    function apri(n) {
      giorni.forEach(function (g, k) { g.classList.toggle('attivo', k === n); g.setAttribute('aria-pressed', k === n); });
      pannelli.forEach(function (p, k) { p.hidden = k !== n; });
    }
    giorni.forEach(function (g, n) { g.addEventListener('click', function () { apri(n); }); });
    box.querySelectorAll('.slot-ora').forEach(function (b) {
      b.addEventListener('click', function () { campo.value = b.dataset.at; aggiorna(); });
    });
    togli.addEventListener('click', function () { campo.value = ''; aggiorna(); });
    if (sede) sede.addEventListener('change', aggiorna);
    aggiorna();
    // si apre il giorno già scelto, altrimenti il primo con slot liberi
    var iniziale = pannelli.findIndex(function (p) { return p.querySelector('.slot-ora.scelto'); });
    if (iniziale < 0) iniziale = giorni.findIndex(function (g) { return !g.classList.contains('pieno'); });
    apri(Math.max(iniziale, 0));
  });

  // Targa: se non ha il formato italiano (AA123AA) compare un avviso con la spunta "È giusta così",
  // obbligatoria per salvare (targa estera o di vecchio formato). Una volta salvata non si cambia.
  var TARGA_IT = /^[A-HJ-NPR-TV-Z]{2}[0-9]{3}[A-HJ-NPR-TV-Z]{2}$/;
  document.querySelectorAll('input[name=targa]').forEach(function (campo) {
    var avviso = document.createElement('label');
    avviso.className = 'spunta avviso-targa';
    avviso.hidden = true;
    avviso.innerHTML = '<input type="checkbox" name="targa_ok" value="1"><span><b>Formato insolito:</b> le targhe italiane sono come GH123AB. ' +
                       'Controllala, oppure spunta se è giusta così (targa estera o di vecchio formato).</span>';
    var spunta = avviso.querySelector('input');
    (campo.closest('label') || campo).insertAdjacentElement('afterend', avviso);
    function controlla() {
      var t = campo.value.toUpperCase().replace(/[^A-Z0-9]/g, '');
      var insolita = t.length > 0 && !TARGA_IT.test(t);
      avviso.hidden = !insolita;
      spunta.required = insolita;
      if (!insolita) spunta.checked = false;
    }
    campo.addEventListener('input', controlla);
    controlla();
  });

  // Pulsanti su una riga (sedi dello Stock, tipi di lavorazione): se i nomi non ci stanno prima si compatta
  // (niente icone, "Tutte"), se ancora non bastano va a capo in colonne uguali invece di tagliare i nomi
  document.querySelectorAll('.filtro-gruppo.in-riga').forEach(function (riga) {
    var nomi = [].slice.call(riga.querySelectorAll('.nome'));
    nomi.forEach(function (n) { n.dataset.lungo = n.textContent; });
    function tagliati() { return nomi.some(function (n) { return n.scrollWidth > n.clientWidth + 1; }); }
    function adatta() {
      riga.classList.remove('stretta', 'a-capo');
      nomi.forEach(function (n) { n.textContent = n.dataset.lungo; });
      if (!tagliati()) return;
      riga.classList.add('stretta');
      nomi.forEach(function (n) { if (n.dataset.corto) n.textContent = n.dataset.corto; });
      if (tagliati()) riga.classList.add('a-capo');
    }
    var attesa;
    window.addEventListener('resize', function () { clearTimeout(attesa); attesa = setTimeout(adatta, 120); });
    adatta();
  });

  // Descrizione della pagina: sul telefono 2 righe, toccandola si legge tutta (solo se è davvero tagliata)
  document.querySelectorAll('.descrizione-pagina').forEach(function (p) {
    if (p.scrollHeight <= p.clientHeight + 2) return;
    p.classList.add('tagliata');
    p.setAttribute('role', 'button');
    p.setAttribute('tabindex', '0');
    p.setAttribute('aria-expanded', 'false');
    function apri() { var a = p.classList.toggle('aperta'); p.setAttribute('aria-expanded', a ? 'true' : 'false'); }
    p.addEventListener('click', apri);
    p.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); apri(); } });
  });
})();
