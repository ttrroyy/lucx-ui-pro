"""New native geodata sources remain optional and version-scoped on install."""
import json,os,re,shutil,sqlite3,subprocess,sys,tempfile,unittest
from pathlib import Path

SOURCE=(Path(__file__).resolve().parents[1]/'lucx-ui-latest.sh').read_text(encoding='utf-8')

def function(name):
    return re.search(r'^'+name+r'\(\) \{.*?^\}',SOURCE,re.M|re.S)[0]

class Geodata306(unittest.TestCase):
    def test_partial_http200_download_preserves_old_file_and_uses_fallback(self):
        text='''curl(){
  printf 200
  if [[ "${!#}" == https://example.invalid/complete ]]; then
    printf COMPLETE > "$2"; return 0
  fi
  printf partial > "$2"; return 28
}
'''+function('download_one_geo')+'''
dest=$(dirname "$0")
printf ORIGINAL > "$dest/test.dat"
if download_one_geo "$dest" test.dat https://example.invalid/partial '' 1; then exit 1; fi
[[ "$(cat "$dest/test.dat")" == ORIGINAL ]] || exit 2
download_one_geo "$dest" test.dat https://example.invalid/partial https://example.invalid/complete 1 || exit 3
[[ "$(cat "$dest/test.dat")" == COMPLETE ]] || exit 4
'''
        self.assertIn('HTTP 200; curl exit 28',self.bash(text))

    def bash(self,text):
        bash=os.environ.get('LUCX_TEST_BASH') or shutil.which('bash')
        if not bash:self.skipTest('bash unavailable')
        with tempfile.TemporaryDirectory() as folder:
            script=Path(folder)/'check.sh';script.write_text(text,encoding='utf-8',newline='\n')
            result=subprocess.run([bash,str(script)],capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            return result.stdout

    def test_version_gate_includes_305_and_future_builds_not_older_panels(self):
        gate=function('panel_supports_ga_geodata').replace('/usr/local/x-ui/x-ui -v','printf "%s" "$test_version"')
        text=gate+'\nfor test_version in 3.9.0-lucx.286 3.9.0-lucx.287 3.10.2-lucx.304 3.10.2-lucx.305 3.10.2-lucx.306 3.11.0-lucx.999 unknown; do\nif panel_supports_ga_geodata; then echo yes; else echo no; fi\ndone\n'
        self.assertEqual(self.bash(text).splitlines(),['no','no','no','yes','yes','yes','no'])

    def test_fresh_template_preserves_custom_fields_and_urls(self):
        section=SOURCE[SOURCE.index('apply_xray_template() {'):SOURCE.index('apply_xray_dns()')]
        body=section.split("<<'PY'\n",1)[1].split('\nPY\n',1)[0]
        for supported in ('0','1'):
            with self.subTest(ga=supported),tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'panel.db'
                custom={'routing':{'domainStrategy':'AsIs','custom':'keep'},'future':{'x':1},'geodata':{'cron':'1 2 * * *','custom':'keep','assets':[{'file':'custom.dat','url':'https://example.invalid/custom.dat'},{'file':'geoip_GA.dat','url':'https://example.invalid/own-ga.dat'}]}}
                with sqlite3.connect(path) as db:
                    db.execute('CREATE TABLE settings(id INTEGER PRIMARY KEY,key TEXT,value TEXT)')
                    db.execute('INSERT INTO settings(key,value) VALUES(?,?)',('xrayTemplateConfig',json.dumps(custom)))
                subprocess.run([sys.executable,'-c',body,str(path),'1','','','0','','','','','',supported],check=True,capture_output=True,text=True)
                with sqlite3.connect(path) as db:result=json.loads(db.execute('SELECT value FROM settings').fetchone()[0])
                self.assertEqual(result['routing'],custom['routing'])
                self.assertEqual(result['future'],custom['future'])
                self.assertEqual(result['geodata']['cron'],'1 2 * * *')
                self.assertEqual(result['geodata']['custom'],'keep')
                assets={x['file']:x['url'] for x in result['geodata']['assets']}
                self.assertEqual(assets['geoip_GA.dat'],'https://example.invalid/own-ga.dat')
                self.assertEqual('geosite_GA.dat' in assets,supported=='1')
                if supported=='1':self.assertEqual(assets['geosite_GA.dat'],'https://raw.githubusercontent.com/Ground-Zerro/Geo-Aggregator/main/geodat/geosite_GA.dat')

    def test_optional_ga_download_failure_does_not_abort_install(self):
        text='panel_supports_ga_geodata(){ return 0; }\nmkdir(){ :; }\nensure_stock_geo(){ printf "%s\\n" "$*"; [[ "$2" != *_GA.dat ]]; }\ndownload_one_geo(){ :; }\n'+function('fetch_lucx_geofiles')+'\nfetch_lucx_geofiles /test || exit 1\necho INSTALL_CONTINUES\n'
        result=self.bash(text)
        self.assertIn('geoip_GA.dat https://raw.githubusercontent.com/Ground-Zerro/Geo-Aggregator/main/geodat/geoip_GA.dat  50000 600',result)
        self.assertIn('geosite_GA.dat',result)
        self.assertIn('geosite_RU.dat https://github.com/runetfreedom/russia-v2ray-rules-dat/releases/latest/download/geosite.dat https://raw.githubusercontent.com/runetfreedom/russia-v2ray-rules-dat/release/geosite.dat 50000 600',result)
        self.assertIn('INSTALL_CONTINUES',result)
        self.assertNotIn('_GA.dat',self.bash(text.replace('panel_supports_ga_geodata(){ return 0; }','panel_supports_ga_geodata(){ return 1; }')))

    def test_existing_geodata_file_is_not_overwritten(self):
        text=function('ensure_stock_geo')+'\ndownload_one_geo(){ echo OVERWRITTEN; return 1; }\nensure_stock_geo "$(dirname "$0")" check.sh https://example.invalid\n'
        self.assertNotIn('OVERWRITTEN',self.bash(text))
