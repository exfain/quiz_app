/* Shared Web-Mercator surface. Artwork uses the same full-world coordinate extent. */
(function () {
    'use strict';
    const NS = 'http://www.w3.org/2000/svg', MAX = 85.05112878;
    const regions = {world_mercator: [-180, -60, 180, 83], germany: [3.3, 45.7, 16.9, 56.4], europe: [-16, 32, 39, 74]};
    const cache = new Map();
    const BASE = '/static/Where_is_this/maps/';
    function canonicalPoint(point) {
        if (point.x_norm == null) return [Number(point.latitude), Number(point.longitude)];
        return [Math.atan(Math.sinh(Math.PI * (1 - 2 * Number(point.y_norm)))) * 180 / Math.PI, Number(point.x_norm) * 360 - 180];
    }
    async function loadMap(region) {
        if (cache.has(region)) return cache.get(region);
        const promise = (async () => {
            const config = await fetch(BASE + 'maps_config.json').then(r => {if (!r.ok) throw Error('Kartenkonfiguration fehlt'); return r.json();});
            const key = region === 'world_mercator' ? 'world' : region, meta = config.maps[key];
            const [data, border] = await Promise.all([fetch(BASE + meta.file).then(r => {if (!r.ok) throw Error('Karte fehlt'); return r.json();}),
                meta.borderFile ? fetch(BASE + meta.borderFile).then(r => {if (!r.ok) throw Error('Grenzlayer fehlt'); return r.json();}) : null]);
            const definition = key === 'germany' ? '+proj=utm +zone=32 +ellps=GRS80 +units=m +no_defs' : '+proj=laea +lat_0=52 +lon_0=10 +x_0=4321000 +y_0=3210000 +ellps=GRS80 +units=m +no_defs';
            const converter = key === 'world' ? null : proj4('EPSG:4326', definition);
            let extent = [0, 0, 1, 1];
            if (converter) {
                const samples = [], [w, south, e, north] = meta.geoBounds;
                for (let i=0;i<=100;i++) {const t=i/100; samples.push(converter.forward([w+(e-w)*t,south]),converter.forward([w+(e-w)*t,north]),converter.forward([w,south+(north-south)*t]),converter.forward([e,south+(north-south)*t]));}
                extent = [Math.min(...samples.map(p=>p[0])),Math.min(...samples.map(p=>p[1])),Math.max(...samples.map(p=>p[0])),Math.max(...samples.map(p=>p[1]))];
            }
            const projection = (lat, lon) => {
                if (!converter) return project(lat,lon);
                const p=converter.forward([lon,lat]); return [(p[0]-extent[0])/(extent[2]-extent[0]),(extent[3]-p[1])/(extent[2]-extent[0])];
            };
            const inverse = (x,y) => {
                if (!converter) return canonicalPoint({x_norm:x,y_norm:y});
                const p=converter.inverse([extent[0]+x*(extent[2]-extent[0]),extent[3]-y*(extent[2]-extent[0])]); return [p[1],p[0]];
            };
            const group=element('g',{'data-map-layer':region});
            function pathLine(coords, closed) {
                let previous=null;
                return coords.map((p,i)=>{
                    const xy=projection(p[1],p[0]);
                    // Split world outlines at the date line instead of crossing the map.
                    const jump=key==='world' && previous!==null && Math.abs(xy[0]-previous)>.5;
                    previous=xy[0]; return (i===0||jump?'M':'L')+xy.join(',');
                }).join(' ')+(closed?' Z':'');
            }
            for (const feature of data.features) {
                const polygons=feature.geometry.type==='MultiPolygon'?feature.geometry.coordinates:[feature.geometry.coordinates];
                for (const polygon of polygons) group.append(element('path',{d:polygon.map(r=>pathLine(r,true)).join(' '),fill:config.palette.land,'fill-rule':'evenodd'}));
            }
            const borders=element('g',{'data-border-layer':region});
            for (const feature of border?.features||[]) {
                const lines=feature.geometry.type==='MultiLineString'?feature.geometry.coordinates:[feature.geometry.coordinates];
                for (const line of lines) borders.append(element('path',{d:pathLine(line,false),fill:'none',stroke:config.palette.germanyBorder,'stroke-width':1.2,'vector-effect':'non-scaling-stroke','data-germany-border':'true'}));
            }
            const height=converter?(extent[3]-extent[1])/(extent[2]-extent[0]):1;
            const imageSvg=element('svg',{xmlns:NS,viewBox:`0 0 1 ${height}`,preserveAspectRatio:'none'});
            imageSvg.append(element('rect',{x:0,y:0,width:1,height,fill:config.palette.water}),group);
            const url=URL.createObjectURL(new Blob([new XMLSerializer().serializeToString(imageSvg)],{type:'image/svg+xml'}));
            return {url,height,borders,project:projection,inverse,meta,water:config.palette.water};
        })();
        cache.set(region,promise); promise.catch(()=>cache.delete(region)); return promise;
    }
    function project(lat, lon) {
        lat = Math.max(-MAX, Math.min(MAX, Number(lat)));
        return [(Number(lon) + 180) / 360, .5 - Math.log(Math.tan(Math.PI / 4 + lat * Math.PI / 360)) / (2 * Math.PI)];
    }
    function element(tag, attrs, text) {
        const node = document.createElementNS(NS, tag);
        for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, value);
        if (text !== undefined) node.textContent = text;
        return node;
    }
    class WhereMap {
        constructor(container, options = {}) {
            this.container = container; this.onSelect = options.onSelect;
            this.artwork = null;
            this.svg = element('svg', {role: 'img', 'aria-label': 'Karte. Verschieben durch Ziehen, Zoom mit Mausrad oder den Schaltflächen.'});
            container.replaceChildren(this.svg); this.pointers = new Map(); this.region = 'world_mercator';
            this.svg.style.cssText = 'width:100%;height:100%;display:block;touch-action:none;background:#b9dcf2';
            this.svg.addEventListener('wheel', e => {e.preventDefault(); this.zoom(e.deltaY < 0 ? .8 : 1.25, e);}, {passive: false});
            this.svg.addEventListener('pointerdown', e => {
                this.svg.setPointerCapture(e.pointerId); this.pointers.set(e.pointerId, [e.clientX, e.clientY]);
                this.down = [e.clientX, e.clientY]; this.moved = false;
            });
            this.svg.addEventListener('pointermove', e => {
                const previous = this.pointers.get(e.pointerId); if (!previous) return;
                const before = [...this.pointers.values()];
                this.pointers.set(e.pointerId, [e.clientX, e.clientY]);
                const after = [...this.pointers.values()];
                if (Math.hypot(e.clientX - this.down[0], e.clientY - this.down[1]) > 5) this.moved = true;
                if (after.length === 2 && before.length === 2) {
                    const distance = ps => Math.hypot(ps[0][0] - ps[1][0], ps[0][1] - ps[1][1]);
                    if (distance(after) > 0) this.zoom(distance(before) / distance(after));
                    this.moved = true;
                } else {
                    const a = this.point(previous[0], previous[1]), b = this.point(e.clientX, e.clientY);
                    this.box[0] += a[0] - b[0]; this.box[1] += a[1] - b[1]; this.clamp(); this.draw();
                }
            });
            this.svg.addEventListener('pointerup', e => {
                const click = !this.moved && this.pointers.size === 1; this.pointers.delete(e.pointerId);
                if (click && this.enabled && this.artwork && this.onSelect) {
                    let [x, y] = this.point(e.clientX, e.clientY);
                    if (y < 0 || y > (this.artwork?.height || 1)) return;
                    if (this.region === 'world_mercator') x = ((x % 1) + 1) % 1;
                    const [west, south, east, north] = regions[this.region] || regions.world_mercator;
                    const [lat,lon] = this.artwork.inverse(x,y);
                    if (this.region !== 'world_mercator' && (lon < west || lon > east || lat < south || lat > north)) return;
                    const [x_norm,y_norm] = project(lat,lon);
                    this.onSelect({x_norm,y_norm,latitude:lat,longitude:lon});
                }
            });
            this.svg.addEventListener('pointercancel', e => {this.pointers.delete(e.pointerId); this.moved = true;});
            this.observer = new ResizeObserver(() => {this.reset();}); this.observer.observe(container);
            this.reset();
        }
        async load() {
            const region=this.region;this.loadingRegion=region; this.artwork=null; this.container.dataset.loaded='false';
            try {const artwork=await loadMap(region); if(this.region!==region)return; this.artwork=artwork;regions[region]=artwork.meta.geoBounds;this.container.dataset.loaded='true';this.svg.style.background=region==='world_mercator'?artwork.water:'#d8e1e8';this.reset();}
            catch(error) {this.container.dataset.loaded='error'; this.svg.replaceChildren(element('text',{x:0,y:.5,'font-size':.02},'Karte konnte nicht geladen werden. Bitte neu laden.'));}
        }
        project(lat,lon) {return this.artwork ? this.artwork.project(lat,lon) : project(lat,lon);}
        copies() {return this.region==='world_mercator' ? Array.from({length:Math.ceil(this.box[0]+this.box[2])-Math.floor(this.box[0])+2},(_,i)=>Math.floor(this.box[0])-1+i) : [0];}
        point(x, y) {
            const p = new DOMPoint(x, y).matrixTransform(this.svg.getScreenCTM().inverse()); return [p.x, p.y];
        }
        reset() {
            if (!this.container.clientWidth || !this.container.clientHeight) return;
            const bounds = regions[this.region] || regions.world_mercator;
            const a = this.region==='world_mercator' ? project(bounds[3],bounds[0]) : [0,0], b = this.region==='world_mercator' ? project(bounds[1],bounds[2]) : [1,this.artwork?.height||1];
            let w = b[0] - a[0], h = b[1] - a[1];
            const ratio = this.container.clientWidth / Math.max(1, this.container.clientHeight);
            if (w / h < ratio) w = h * ratio; else h = w / ratio;
            this.box = [(a[0] + b[0] - w) / 2, (a[1] + b[1] - h) / 2, w, h];
            this.defaultBox = [...this.box]; this.draw();
        }
        zoom(factor, event) {
            if (!this.box) return;
            const old = this.box, w = Math.max(this.defaultBox[2] / 32, Math.min(this.defaultBox[2], old[2] * factor));
            factor = w / old[2];
            const anchor = event ? this.point(event.clientX, event.clientY) : [old[0] + old[2] / 2, old[1] + old[3] / 2];
            this.box = [anchor[0] - (anchor[0] - old[0]) * factor, anchor[1] - (anchor[1] - old[1]) * factor, w, old[3] * factor];
            this.clamp(); this.draw();
        }
        clamp() {
            const b = this.box;
            b[1] = b[3] >= 1 ? (1 - b[3]) / 2 : Math.min(1 - b[3], Math.max(0, b[1]));
            if (this.region !== 'world_mercator') {
                b[0] = Math.max(this.defaultBox[0], Math.min(this.defaultBox[0] + this.defaultBox[2] - b[2], b[0]));
                b[1] = Math.max(this.defaultBox[1], Math.min(this.defaultBox[1] + this.defaultBox[3] - b[3], b[1]));
            }
        }
        update(state, {ownName = null, spectator = false, draft = null} = {}) {
            const region = state.question?.map_type || 'world_mercator';
            const key = `${state.current_round_id}:${state.phase === 'reveal'}:${region}`;
            if (this.region!==region || this.loadingRegion!==region) {this.region=region;this.load();}
            if (this.key !== key) {this.key = key; this.reset();}
            this.state = state; this.ownName = ownName; this.spectator = spectator; this.draft = draft;
            this.enabled = !!state.can_answer && !spectator; this.draw();
        }
        marker(point, fill, label, role) {
            const xy = this.project(...canonicalPoint(point));
            const radius = this.box[2] / Math.max(1, this.container.clientWidth) * 7;
            for (const copy of this.copies()) {
                const x = xy[0] + copy, y = xy[1];
                this.svg.append(element('circle', {cx: x, cy: y, r: radius, fill, stroke: '#fff', 'stroke-width': radius / 4, 'data-marker': role}));
                if (label) this.svg.append(element('text', {x: x + radius * 1.4, y: y - radius * 1.4, fill: '#102338', 'font-size': radius * 2, 'paint-order': 'stroke', stroke: '#fff', 'stroke-width': radius / 3, 'data-team-label': role}, label));
            }
        }
        line(answer, target) {
            const a = this.project(...canonicalPoint(answer));
            const b = this.project(target.latitude, target.longitude);
            while (this.region==='world_mercator' && a[0] - b[0] > .5) a[0] -= 1;
            while (this.region==='world_mercator' && a[0] - b[0] < -.5) a[0] += 1;
            const scale = this.box[2] / Math.max(1, this.container.clientWidth);
            for (const copy of this.copies()) {
                this.svg.append(element('line', {x1: a[0] + copy, y1: a[1], x2: b[0] + copy, y2: b[1], stroke: '#cf3c39', 'stroke-width': scale * 3, 'data-distance-line': 'true'}));
                this.svg.append(element('text', {x: (a[0] + b[0]) / 2 + copy, y: (a[1] + b[1]) / 2, 'text-anchor': 'middle', 'font-size': scale * 15, 'paint-order': 'stroke', stroke: '#fff', 'stroke-width': scale * 4, fill: '#102338', 'data-distance-label': 'true'}, answer.formatted_distance));
            }
        }
        ring(target, radius, color) {
            const lat = target.latitude * Math.PI / 180, lon = target.longitude * Math.PI / 180, d = Math.min(Math.PI, radius / 6371), points = [];
            const center = this.project(target.latitude, target.longitude)[0];
            let previous = center;
            for (let i = 0; i <= 180; i++) {
                const angle = i * 2 * Math.PI / 180;
                const phi = Math.asin(Math.sin(lat) * Math.cos(d) + Math.cos(lat) * Math.sin(d) * Math.cos(angle));
                const lambda = lon + Math.atan2(Math.sin(angle) * Math.sin(d) * Math.cos(lat), Math.cos(d) - Math.sin(lat) * Math.sin(phi));
                const p = this.project(phi * 180 / Math.PI, lambda * 180 / Math.PI);
                while (this.region==='world_mercator' && p[0] - previous > .5) p[0] -= 1;
                while (this.region==='world_mercator' && p[0] - previous < -.5) p[0] += 1;
                previous = p[0]; points.push(p);
            }
            for (const copy of this.copies())
                this.svg.append(element('polygon', {points: points.map(p => `${p[0] + copy},${p[1]}`).join(' '), fill: color, 'fill-opacity': .14, stroke: color, 'stroke-width': this.box[2] / Math.max(1, this.container.clientWidth) * 2, 'data-zone-radius': radius}));
        }
        draw() {
            if (!this.box) return;
            this.svg.setAttribute('viewBox', this.box.join(' ')); this.svg.replaceChildren();
            for (const copy of this.copies())
                if(this.artwork)this.svg.append(element('image',{href:this.artwork.url,x:copy,y:0,width:1,height:this.artwork.height,preserveAspectRatio:'none','data-map-region':this.region}));
            if(this.artwork) this.svg.append(this.artwork.borders.cloneNode(true));
            const s = this.state; if (!s) return;
            if (s.phase !== 'reveal') {
                const point = s.own_answer || this.draft || s.pending_input;
                if (point && !this.spectator) this.marker(point, '#2867d2', null, 'own');
                return;
            }
            const target = s.question?.correct_location; if (!target) return;
            const answers = s.answers || [], winner = s.scoring_mode === 'rank' ? answers.find(a => a.rank_position === 1) : null;
            if (s.scoring_mode === 'zones') for (const [i, z] of [...(s.zone_scoring?.zones || [])].reverse().entries()) this.ring(target, z.max_distance_km, ['#d76d30', '#429161', '#2767bb'][i % 3]);
            const own = answers.find(a => a.participant_name === this.ownName);
            const line = this.spectator ? (s.scoring_mode === 'rank' ? winner : null) : own;
            if (line) this.line(line, target);
            for (const answer of this.spectator ? answers : own ? [own] : [])
                this.marker(answer, answer === winner ? '#e99b15' : '#2867d2', this.spectator && answer === winner ? answer.participant_name : null, this.spectator && answer === winner ? 'winner' : 'guess');
            this.marker(target, '#d33539', 'Ziel', 'target');
        }
        destroy() {this.observer.disconnect(); this.container.replaceChildren();}
    }
    window.WhereMap = WhereMap;
})();
