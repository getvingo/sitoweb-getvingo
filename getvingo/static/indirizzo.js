// Indirizzo verificato sulla mappa (macro campi_indirizzo): la via si cerca da sola (OpenStreetMap),
// CAP, provincia e stato si compilano; toccando la mappa o "Sono qui" si compila tutto dal punto.
// Se la via non esiste: contorno rosso e il modulo non parte (anche il server rifiuta senza coordinate).
(function () {
  var blocchi = document.querySelectorAll('[data-indirizzo]');
  if (!blocchi.length) return;
  var NOMINATIM = 'https://nominatim.openstreetmap.org/';
  var leaflet = new Promise(function (ok) {
    if (window.L) return ok();
    var css = document.createElement('link'); css.rel = 'stylesheet';
    css.href = 'https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css'; document.head.appendChild(css);
    var js = document.createElement('script'); js.src = 'https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js';
    js.onload = ok; document.head.appendChild(js);
  });

  blocchi.forEach(function (box) {
    var form = box.closest('form');
    var c = function (n) { return box.querySelector('[data-campo=' + n + ']'); };
    var lat = box.querySelector('[name=lat]'), lon = box.querySelector('[name=lon]');
    var esito = box.querySelector('[data-esito]');
    var obbligatorio = box.hasAttribute('data-obbligatorio');
    var mappa = null, segnaposto = null, timer = null, ultima = '';

    function stato(testo, tipo) {
      esito.textContent = testo || '';
      esito.className = 'indirizzo-esito ' + (tipo || '');
      c('indirizzo').classList.toggle('campo-errore', tipo === 'errore');
    }
    function verificato(la, lo) {
      lat.value = la ? Number(la).toFixed(6) : ''; lon.value = lo ? Number(lo).toFixed(6) : '';
      if (!la && segnaposto && mappa) { mappa.removeLayer(segnaposto); segnaposto = null; }
      if (!la || !mappa) return;
      if (!segnaposto) {
        segnaposto = L.marker([la, lo], { draggable: true }).addTo(mappa);
        segnaposto.on('dragend', function () { var p = segnaposto.getLatLng(); daPunto(p.lat, p.lng); });
      } else segnaposto.setLatLng([la, lo]);
      mappa.setView([la, lo], Math.max(mappa.getZoom(), 17));
    }
    function provincia(a) { return (a['ISO3166-2-lvl6'] || '').replace(/^IT-/, ''); }
    function comuneDi(a) { return a.municipality || a.city || a.town || a.village || ''; }
    // "Via Roma, n. 12/a" → strada "Via Roma", civico "12/A" (anche 12 bis, snc). Il numero deve stare in fondo:
    // "Via 4 Novembre" non ha civico.
    function separa(via) {
      var v = (via || '').replace(/\s+/g, ' ').trim();
      if (/[\s,]s\.?\s?n\.?\s?c\.?$/i.test(v)) return { strada: v.replace(/[\s,]+s\.?\s?n\.?\s?c\.?$/i, ''), civico: 'snc' };
      var m = v.match(/^(.*?\D)[\s,]+(?:n(?:r|um(?:ero)?)?\s?[.°º]?\s*)?(\d+)\s*(?:\/?\s*([a-z])|\s+(bis|ter|quater))?$/i);
      if (!m) return { strada: v, civico: '' };
      return { strada: m[1].replace(/[\s,]+$/, ''), civico: m[2] + (m[3] ? '/' + m[3].toUpperCase() : m[4] ? ' ' + m[4].toLowerCase() : '') };
    }
    function numero(civ) { return ((civ || '').split(/[;,]/)[0].match(/\d+/) || [''])[0]; }  // 12/A, "12;14" → 12
    function semplice(t) { return (t || '').toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/\(.*?\)/g, '').replace(/[^a-z0-9]/g, ''); }
    function stessoComune(a, comune) {
      var c0 = semplice(comune);
      return [a.municipality, a.city, a.town, a.village, a.hamlet, a.suburb].some(function (x) { return x && semplice(x) === c0; });
    }
    function compila(a, sovrascrivi) {
      var imposta = function (n, v) { if (v && (sovrascrivi || !c(n).value)) c(n).value = v; };
      imposta('comune', comuneDi(a)); imposta('cap', a.postcode); imposta('provincia', provincia(a));
      imposta('stato', a.country);
    }
    function cerca() {
      var via = c('indirizzo').value.trim(), comune = c('comune').value.trim();
      if (!via || !comune) return;
      var chiave = via + '|' + comune + '|' + c('stato').value;
      if (chiave === ultima) return;
      ultima = chiave;
      stato('Cerco l’indirizzo…');
      if (GetvingoMappa.google) {
        return cercaGoogle(via + ', ' + comune + ', ' + (c('stato').value || 'Italia'), null, true).then(function (ris) {
          var r = ris[0];
          if (!r || !r.via) { verificato(null); return stato('Via non trovata a ' + comune + ': controlla come è scritta, oppure tocca il punto sulla mappa.', 'errore'); }
          if (r.comune && semplice(r.comune) !== semplice(comune)) {
            verificato(null); return stato('Via non trovata a ' + comune + ': controlla come è scritta, oppure tocca il punto sulla mappa.', 'errore');
          }
          c('cap').value = r.cap || c('cap').value;
          c('provincia').value = r.provincia || c('provincia').value; c('stato').value = r.stato || c('stato').value;
          verificato(r.lat, r.lon);
          var pg = separa(via), ng = numero(pg.civico);
          stato(!pg.civico ? 'Via trovata · aggiungi il numero civico' : (numero(separa(r.via).civico) === ng || pg.civico === 'snc') ? 'Indirizzo trovato ✓' :
                'Via trovata ✓ · il civico ' + pg.civico + ' non è sulla mappa: il punto è sulla via, spostalo sul numero giusto se serve', 'ok');
        }).catch(function () { ultima = ''; stato('Ricerca non riuscita: riprova o tocca il punto sulla mappa.', 'errore'); });
      }
      // OpenStreetMap legge "Treviso" anche come provincia e restituisce la stessa via di un altro comune
      // (Via Roma 12 a Castelfranco): si tengono solo i risultati del comune scritto. Prima con il civico,
      // poi la sola via; il comune scritto non si sovrascrive mai.
      var parti = separa(via), num = numero(parti.civico);
      var tentativi = [num ? num + ' ' + parti.strada : '', parti.strada, via]
        .filter(function (t, i, l) { return t && l.indexOf(t) === i; });
      function prova(i) {
        if (i >= tentativi.length) return Promise.resolve(null);
        var q = new URLSearchParams({ format: 'jsonv2', addressdetails: 1, limit: 10, 'accept-language': 'it',
                                      street: tentativi[i], city: comune, country: c('stato').value || 'Italia' });
        return fetch(NOMINATIM + 'search?' + q).then(function (r) { return r.json(); }).then(function (ris) {
          var qui = (ris || []).filter(function (r) { return r.address && r.address.road && stessoComune(r.address, comune); });
          if (!qui.length) return prova(i + 1);
          return qui.filter(function (r) { return num && numero(r.address.house_number) === num; })[0] || qui[0];
        });
      }
      prova(0).then(function (r) {
        if (chiave !== ultima) return;  // nel frattempo l'indirizzo è cambiato
        if (!r) {
          verificato(null);
          return stato('Via non trovata a ' + comune + ': controlla come è scritta, oppure tocca il punto sulla mappa.', 'errore');
        }
        var a = r.address, trovato = num && numero(a.house_number) === num;
        ['cap', 'provincia', 'stato'].forEach(function (n) {
          var v = n === 'cap' ? a.postcode : n === 'provincia' ? provincia(a) : a.country;
          if (v) c(n).value = v;
        });
        // via scritta come sulla mappa e civico in forma pulita (12/A), se non la si sta ancora scrivendo
        if (document.activeElement !== c('indirizzo')) {
          c('indirizzo').value = a.road + (parti.civico ? ' ' + parti.civico : '');
          ultima = c('indirizzo').value + '|' + c('comune').value + '|' + c('stato').value;
        }
        verificato(r.lat, r.lon);
        if (!parti.civico) return stato('Via trovata · aggiungi il numero civico', 'ok');
        stato(trovato || parti.civico === 'snc' ? 'Indirizzo trovato ✓' :
              'Via trovata ✓ · il civico ' + parti.civico + ' non è sulla mappa: il punto è sulla via, spostalo sul numero giusto se serve', 'ok');
      }).catch(function () { ultima = ''; stato('Ricerca non riuscita: riprova o tocca il punto sulla mappa.', 'errore'); });
    }
    function daPunto(la, lo) {
      if (c('indirizzo').value.trim() && c('comune').value.trim()) {
        // indirizzo già scritto: si corregge solo il punto sulla mappa, via e civico restano quelli
        verificato(la, lo);
        c('indirizzo').classList.remove('campo-errore');
        return stato('Punto aggiornato ✓ · via e civico restano quelli scritti', 'ok');
      }
      stato('Leggo l’indirizzo del punto…');
      fetch(NOMINATIM + 'reverse?format=jsonv2&addressdetails=1&zoom=18&accept-language=it&lat=' + la + '&lon=' + lo)
        .then(function (r) { return r.json(); }).then(function (d) {
          var a = d && d.address;
          if (!a || !a.road) return stato('Qui non c’è una via: tocca più vicino alla strada.', 'errore');
          var civ = a.house_number ? separa('x ' + a.house_number.split(/[;,]/)[0]).civico || a.house_number : '';
          c('indirizzo').value = a.road + (civ ? ' ' + civ : '');
          compila(a, true);
          ultima = c('indirizzo').value + '|' + c('comune').value + '|' + c('stato').value;
          verificato(la, lo);
          stato(a.house_number ? 'Indirizzo compilato dal punto ✓' : 'Via compilata dal punto ✓ · aggiungi il numero civico', 'ok');
        }).catch(function () { stato('Lettura non riuscita: riprova.', 'errore'); });
    }

    // ---- "Cerca un luogo": nome di un'azienda o indirizzo; scelto un risultato si compila tutto
    var cercaIn = box.querySelector('[data-cerca]'), lista = box.querySelector('[data-risultati]');
    var campoNome = form && (form.querySelector('[name=nome]') || form.querySelector('[name=nuova_destinazione]') ||
                             form.querySelector('[name=ragione_sociale]'));
    function riempi(r) {
      lista.hidden = true; lista.innerHTML = '';
      c('indirizzo').value = r.via; c('comune').value = r.comune || c('comune').value;
      c('cap').value = r.cap || ''; c('provincia').value = r.provincia || ''; c('stato').value = r.stato || 'Italia';
      if (campoNome && !campoNome.value.trim() && r.nome) campoNome.value = r.nome;
      ultima = c('indirizzo').value + '|' + c('comune').value + '|' + c('stato').value;
      verificato(r.lat, r.lon);
      stato(r.via ? 'Luogo trovato ✓ · controlla i dati' + (/\d/.test(r.via) ? '' : ' e aggiungi il civico') : 'Luogo trovato: scrivi via e civico', r.via ? 'ok' : 'errore');
    }
    function componenti(comp) {
      var t = {}, breve = {};
      (comp || []).forEach(function (x) { (x.types || []).forEach(function (ty) { t[ty] = x.longText; breve[ty] = x.shortText; }); });
      return { via: [t.route, t.street_number].filter(Boolean).join(' '), cap: t.postal_code,
               comune: t.administrative_area_level_3 || t.locality, provincia: breve.administrative_area_level_2, stato: t.country };
    }
    // punto da cui cercare i luoghi più vicini: segnaposto, poi la zona della mappa guardata, poi il telefono
    var mappaMossa = false;
    function riferimento() {
      if (segnaposto) { var p = segnaposto.getLatLng(); return Promise.resolve([p.lat, p.lng]); }
      if (mappa && mappaMossa) { var c0 = mappa.getCenter(); return Promise.resolve([c0.lat, c0.lng]); }
      return new Promise(function (ok) {
        if (!navigator.geolocation) return ok(mappa ? [mappa.getCenter().lat, mappa.getCenter().lng] : null);
        navigator.geolocation.getCurrentPosition(function (p) { ok([p.coords.latitude, p.coords.longitude]); },
          function () { ok(mappa ? [mappa.getCenter().lat, mappa.getCenter().lng] : null); },
          { enableHighAccuracy: false, timeout: 6000, maximumAge: 600000 });
      });
    }
    function km(a, b) {
      var r = Math.PI / 180, dLa = (b[0] - a[0]) * r, dLo = (b[1] - a[1]) * r;
      var h = Math.sin(dLa / 2) * Math.sin(dLa / 2) + Math.cos(a[0] * r) * Math.cos(b[0] * r) * Math.sin(dLo / 2) * Math.sin(dLo / 2);
      return 12742 * Math.asin(Math.sqrt(h));
    }
    function cercaGoogle(q, rif, perVerifica) {
      var corpo = { textQuery: q, languageCode: 'it', regionCode: 'IT', maxResultCount: perVerifica ? 1 : 20 };
      if (rif && !perVerifica) corpo.locationBias = { circle: { center: { latitude: rif[0], longitude: rif[1] }, radius: 50000 } };
      return fetch('https://places.googleapis.com/v1/places:searchText', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Goog-Api-Key': GetvingoMappa.chiave,
          'X-Goog-FieldMask': 'places.displayName,places.formattedAddress,places.addressComponents,places.location' },
        body: JSON.stringify(corpo)
      }).then(function (r) { return r.json(); }).then(function (d) {
        return (d.places || []).map(function (p) {
          var a = componenti(p.addressComponents);
          a.nome = p.displayName && p.displayName.text; a.testo = p.formattedAddress;
          a.lat = p.location.latitude; a.lon = p.location.longitude; return a;
        });
      });
    }
    function cercaOsm(q, rif) {
      // prima solo nella zona intorno al punto di riferimento (circa 40 km); se lì c'è poco, ovunque
      function chiedi(zona) {
        var parametri = { format: 'jsonv2', addressdetails: 1, limit: 20, 'accept-language': 'it', q: q };
        if (zona) {
          parametri.viewbox = [rif[1] - 0.5, rif[0] + 0.35, rif[1] + 0.5, rif[0] - 0.35].join(',');
          parametri.bounded = 1;
        }
        return fetch(NOMINATIM + 'search?' + new URLSearchParams(parametri)).then(function (r) { return r.json(); });
      }
      return (rif ? chiedi(true) : Promise.resolve([])).then(function (vicini) {
        return vicini.length >= 3 ? vicini : chiedi(false).then(function (altri) { return vicini.concat(altri); });
      }).then(function (ris) {
        var visti = {};
        return ris.filter(function (r) { return !visti[r.place_id] && (visti[r.place_id] = 1); }).map(function (r) {
          var a = r.address || {};
          return { nome: r.name, testo: r.display_name, via: [a.road, a.house_number].filter(Boolean).join(' '),
                   cap: a.postcode, comune: comuneDi(a), provincia: provincia(a), stato: a.country, lat: r.lat, lon: r.lon };
        });
      });
    }
    function cercaLuogo() {
      var q = cercaIn.value.trim();
      if (q.length < 3) return;
      lista.hidden = false; lista.innerHTML = '<li class="tenue">Cerco i luoghi più vicini…</li>';
      var rif = null;
      riferimento().then(function (p) {
        rif = p;
        return GetvingoMappa.google ? cercaGoogle(q, rif) : cercaOsm(q, rif);
      }).then(function (ris) {
        lista.innerHTML = '';
        if (!ris.length) { lista.innerHTML = '<li class="tenue">Nessun luogo trovato: prova con altre parole o con il comune.</li>'; return; }
        if (rif) {  // dal più vicino al più lontano
          ris.forEach(function (r) { r.km = km(rif, [Number(r.lat), Number(r.lon)]); });
          ris.sort(function (a, b) { return a.km - b.km; });
        }
        ris.slice(0, 10).forEach(function (r) {
          var li = document.createElement('li'), b = document.createElement('button');
          b.type = 'button'; b.className = 'risultato-luogo';
          var t = document.createElement('b'); t.textContent = r.nome || r.via; b.appendChild(t);
          var s2 = document.createElement('span');
          s2.textContent = (r.km != null ? (r.km < 1 ? Math.round(r.km * 1000) + ' m' : r.km.toFixed(1).replace('.', ',') + ' km') + ' · ' : '') + r.testo;
          b.appendChild(s2);
          b.addEventListener('click', function () { riempi(r); });
          li.appendChild(b); lista.appendChild(li);
        });
      }).catch(function () { lista.innerHTML = '<li class="tenue">Ricerca non riuscita: riprova.</li>'; });
    }
    if (cercaIn) {
      box.querySelector('[data-cerca-vai]').addEventListener('click', cercaLuogo);
      cercaIn.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); cercaLuogo(); } });
    }

    leaflet.then(function () {
      var div = box.querySelector('[data-mappa]');
      mappa = L.map(div, { zoomControl: true, scrollWheelZoom: false }).setView([45.67, 12.24], 9);
      GetvingoMappa.sfondo(mappa);
      mappa.on('click', function (e) { daPunto(e.latlng.lat, e.latlng.lng); });
      mappa.on('dragend zoomend', function () { mappaMossa = true; });
      if (lat.value && lon.value) { verificato(lat.value, lon.value); stato('Indirizzo sulla mappa ✓', 'ok'); }
      else if (c('indirizzo').value && c('comune').value) cerca();
    });

    ['indirizzo', 'comune', 'stato'].forEach(function (n) {
      c(n).addEventListener('input', function () {
        verificato(null); stato('');
        clearTimeout(timer); timer = setTimeout(cerca, 1200);  // una ricerca quando si smette di scrivere
      });
      c(n).addEventListener('change', function () { clearTimeout(timer); cerca(); });
    });
    box.querySelector('[data-sono-qui]').addEventListener('click', function () {
      if (!navigator.geolocation) return stato('Questo browser non dà la posizione.', 'errore');
      stato('Rilevo la posizione…');
      navigator.geolocation.getCurrentPosition(function (p) { daPunto(p.coords.latitude, p.coords.longitude); },
        function (e) { stato(e.code === 1 ? 'Hai negato la posizione: consentila nel browser e riprova.' : 'Posizione non disponibile.', 'errore'); },
        { enableHighAccuracy: true, timeout: 20000, maximumAge: 0 });
    });
    if (form) form.addEventListener('submit', function (e) {
      var vuoto = !c('indirizzo').value.trim() && !c('comune').value.trim();
      if (lat.value || (!obbligatorio && vuoto)) return;
      e.preventDefault(); e.stopImmediatePropagation();
      if (!esito.classList.contains('errore')) stato('Indirizzo da verificare: controlla via e comune, oppure tocca il punto sulla mappa.', 'errore');
      box.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }, true);
  });
})();
