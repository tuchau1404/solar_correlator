"""Shared validator: C++ invokes the same offline loader, not a second schema."""
import hashlib,json,math,pathlib,re
from zoneinfo import ZoneInfo
SCHEMA=pathlib.Path(__file__).resolve().parents[2]/'config'/'solar_storage.schema.json'

def check(value,spec,path='$'):
    types={'object':dict,'array':list,'string':str,'boolean':bool,'integer':int,'number':(int,float)}
    typ=spec.get('type')
    if typ and (not isinstance(value,types[typ]) or (typ in ('integer','number') and isinstance(value,bool))):raise ValueError(f'{path}: expected {typ}')
    if isinstance(value,(int,float)) and not isinstance(value,bool) and not math.isfinite(value):raise ValueError(f'{path}: nonfinite')
    if 'const' in spec and value!=spec['const']:raise ValueError(f'{path}: unsupported version')
    if 'enum' in spec and value not in spec['enum']:raise ValueError(f'{path}: expected {spec["enum"]}')
    if 'minimum' in spec and value<spec['minimum']:raise ValueError(f'{path}: below minimum')
    if typ=='object':
        if set(value)!=set(spec['required']):raise ValueError(f'{path}: missing={set(spec["required"])-set(value)}, unknown={set(value)-set(spec["required"])}')
        for k,v in value.items():check(v,spec['properties'][k],path+'.'+k)
    if typ=='array':
        if not spec['minItems']<=len(value)<=spec['maxItems']:raise ValueError(f'{path}: wrong array length')
        for v in value:check(v,spec['items'],path+'[]')

def load(path):
    path=pathlib.Path(path).resolve(strict=True)
    def unique(pairs):
        d={}
        for k,v in pairs:
            if k in d:raise ValueError('duplicate JSON key '+k)
            d[k]=v
        return d
    c=json.loads(path.read_text(),object_pairs_hook=unique);check(c,json.loads(SCHEMA.read_text()))
    for g,group in c.items():
        if not isinstance(group,dict):continue
        for k,v in group.items():
            if isinstance(v,(int,float)) and not isinstance(v,bool) and (k.endswith('_sec') or k.endswith('_bytes') or k in ('days','roll_minutes','dpi','min_dpi','queue_capacity_records','append_batch_records','chunk_rows','max_attempts','max_pixels','max_bytes','audit_max_rows','tombstone_max_rows')) and v<=0:raise ValueError(f'{g}.{k} must be positive')
    for g in ('storage','quicklook','sync','startup_check'):
        prefix='io_' if g=='storage' else ''
        if c[g][prefix+'backoff_max_sec']<c[g][prefix+'backoff_initial_sec']:raise ValueError(g+': backoff maximum below initial')
    s,b,q=c['storage'],c['budget'],c['quicklook']
    if s['save_freq_bins']<1:raise ValueError('save_freq_bins must be positive')
    if s['chunk_rows']*s['save_freq_bins']*16>=s['max_file_bytes']:raise ValueError('a raw chunk cannot fit reserved HDF5 cap')
    if s['append_batch_records']>s['queue_capacity_records']:raise ValueError('batch exceeds configured bounded record capacity')
    if q['max_pixels']<s['save_freq_bins']:raise ValueError('quicklook pixel budget below one frequency row')
    if s['compression']=='none' and s['compression_level']!=0:raise ValueError('none compression requires level=0')
    if not 0<=s['compression_level']<=9:raise ValueError('compression level outside 0..9')
    shares=[b[k] for k in ('hdf5_share','png_share','auxiliary_share')]
    if any(v<=0 or v>=1 for v in shares) or abs(sum(shares)-1)>1e-12:raise ValueError('budget shares must be positive and sum to 1')
    if b['resume_free_bytes']<=b['min_free_bytes']:raise ValueError('disk hysteresis invalid')
    if s['roll_minutes']*60>=c['retention']['days']*86400:raise ValueError('roll must be shorter than retention')
    if c['startup_check']['enabled'] and not c['sync']['enabled']:raise ValueError('startup check requires sync')
    if q['worker_limit']!=1 or c['sync']['transfers']!=1:raise ValueError('version 1 supports one plot/transfer worker')
    if not 0<=q['coherence_threshold']<=1:raise ValueError('coherence threshold outside 0..1')
    if not 0<=q['percentiles'][0]<q['percentiles'][1]<=100:raise ValueError('invalid color percentiles')
    if q['vmin_db']>=q['vmax_db'] or q['roi_hz'][0]>=q['roi_hz'][1]:raise ValueError('invalid color/ROI bounds')
    if q['min_dpi']>q['dpi'] or min(q['figure_inches'])<=0:raise ValueError('invalid figure dimensions')
    if c['sync']['date_subdir_format']!='%Y-%m-%d':raise ValueError('v1 supports observation-date YYYY-MM-DD only')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*',c['sync']['remote_name']):raise ValueError('invalid remote name')
    for g,k in [('sync','remote_base_dir'),('startup_check','remote_probe_dir')]:
        v=c[g][k]
        if not v or v.startswith(('/','-')) or any(p in ('','.','..') for p in v.split('/')) or '\\' in v or ':' in v:raise ValueError(f'unsafe {g}.{k}')
    for tz in ('observation_timezone','display_timezone'):ZoneInfo(c['time'][tz])
    for g,k in [('storage','data_dir'),('ledger','path'),('ledger','migration_lock_path'),('sync','lock_path'),('sync','manifest_path'),('retention','lock_path'),('logging','path'),('status','path'),('sync','rclone_config_path')]:
        if c[g][k]=='':
            if (g,k)==('sync','rclone_config_path'):continue
            raise ValueError(f'{g}.{k}: empty path')
        p=pathlib.Path(c[g][k]);p=p if p.is_absolute() else path.parent/p
        # Existing symlink components are rejected before canonicalization.
        if any(x.is_symlink() for x in [p,*p.parents]):raise ValueError('symlink config path '+str(p))
        c[g][k]=str(p.resolve())
    data=pathlib.Path(s['data_dir'])
    if data in (pathlib.Path('/'),pathlib.Path.home()) or data.parts[1:2] in [('etc',),('usr',),('bin',),('sbin',),('proc',),('sys',),('dev',),('boot',)]:raise ValueError('unsafe data_dir')
    for g,k in [('ledger','path'),('ledger','migration_lock_path'),('sync','lock_path'),('retention','lock_path'),('status','path'),('logging','path')]:
        if pathlib.Path(c[g][k]).is_relative_to(data):raise ValueError('state/locks must be outside data_dir')
    if c['ledger']['path']!=c['sync']['manifest_path']:raise ValueError('manifest must be the single shared ledger')
    if c['retention']['reader_timeout_sec']<max(2*c['sync']['timeout_sec'],3*c['startup_check']['timeout_sec']):raise ValueError('reader timeout below transfer timeout')
    if c['status']['stale_after_sec']<=c['status']['interval_sec']:raise ValueError('invalid status freshness')
    if s['max_file_bytes']>int(b['daily_limit_bytes']*b['hdf5_share']):raise ValueError('one file exceeds HDF5 daily allocation')
    if q['max_png_bytes']>int(b['daily_limit_bytes']*b['png_share']):raise ValueError('one image exceeds PNG allocation')
    if 2*c['ledger']['max_bytes']+b['metadata_headroom_bytes']>int(b['daily_limit_bytes']*b['auxiliary_share']):raise ValueError('ledger/journal reserve exceeds auxiliary budget')
    auxiliary_live = c['ledger']['max_bytes'] + (c['logging']['rotation_count']+1)*c['logging']['rotation_bytes'] + 8*c['status']['max_bytes'] + c['startup_check']['probe_size_bytes']
    if auxiliary_live > b['metadata_headroom_bytes']:raise ValueError('auxiliary reserve cannot hold backup/log/status/probe maxima')
    if (c['logging']['rotation_count']+1)*c['logging']['rotation_bytes']>c['logging']['daily_limit_bytes']:raise ValueError('log allocation exceeds daily log limit')
    canonical=json.dumps(c,sort_keys=True,separators=(',',':'),allow_nan=False)
    return c,hashlib.sha256(canonical.encode()).hexdigest()
