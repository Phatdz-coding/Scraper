"""
this is a common component to track document/page info: page title, file/document name & total pages
"""

from playwright.async_api import Browser, ConsoleMessage, Page, async_playwright
from html.parser import HTMLParser
import re
from urllib.parse import parse_qs, urlparse

from scrape.classifiers import DocumentType, WebsiteType


class _TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._inside_title = False
        self._title_found = False
        self.title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "title" and not self._inside_title and not self._title_found:
            self._inside_title = True

    def handle_data(self, data: str) -> None:
        if self._inside_title:
            self.title_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._title_found = True
            self._inside_title = False


class Template:
    def __init__(
        self,
        page: Page | None,
        url: str | None = None,
        *,
        website_type: WebsiteType | None = None,
        document_type: DocumentType | None = None,
        file_id: str | None = None,
    ) -> None:
        self.page = page
        self.url = url
        if website_type is not None:
            self.website_type = website_type
        else:
            candidate_url = url or (getattr(page, "url", None) if page else None)
            self.website_type = (
                self.__match_domain(candidate_url) if candidate_url else None  # type: ignore[assignment]
            )
        self.document_type = document_type  # type: ignore[assignment]
        self.fileID = file_id if file_id is not None else self.get_fileID(url=url)

    @classmethod
    async def create(
        cls,
        page: Page,
        url: str | None = None,
        *,
        website_type: WebsiteType | None = None,
        document_type: DocumentType | None = None,
    ) -> "Template":
        """Async factory that resolves website_type, document_type, and fileID from an async Page."""
        instance = cls(
            page,
            url=url,
            website_type=website_type,
            document_type=document_type,
        )
        if instance.website_type is None:
            instance.website_type = await instance.get_website_type(url=url)
        if instance.document_type is None or instance.document_type in {
            DocumentType.SHORTCUT,
            DocumentType.UNKNOWN,
        }:
            instance.document_type = await instance.__check_document_type(page)
        instance.fileID = instance.get_fileID(url=url)
        return instance

    async def __check_document_type(self, page: Page) -> DocumentType:
        """Determine the document type from viewer metadata or page structure."""
        if self.website_type == WebsiteType.GOOGLE_DRIVE:
            # in google drive template
            # there is a element in the main <head> element of the html look like this:
            # <script nonce="">window.viewerData = {config: {'id': '1gtztV1jowueefbYnciIxr4y1SJ-cXojn', 'title': 'name.pptx', 'isItemTrashed':  false , 'documentResourceKey': '','disableTexmexShareClient': true,}, configJson: ["0",null,null,null,null,1,null,null,null,null,1,[1,"https://clients6.google.com",null,"AIzaSyDVQw45DwoYh632gvsP5vPDqEKvb-Ywnb8",0,null,null,null,null,"AIzaSyC1eQ1xj69IdTMeii5r7brs3R90eck-m7k",null,"/drive/v2internal",1,1,null,null,null,null,"/drive/v2internal",1],null,null,null,"https://drive.google.com",null,null,"https://drive.google.com",null,null,null,null,null,null,null,null,null,null,1,null,null,[["20261029.00_p2"],6,1,1,null,null,null,1,["ADFN-cul2ZPENas3yUiHJhqoi71CDrJKJGo6NVYwwJoX7l_CW4S7TmnIFZofoP1631xGpd7DsWA6",1,"CJ-0psiS_pcDFX_CzgAdgZcBjw",1794339240516126,[5703839,5704621,5704693,5704745,5705891,5706786,5713195,5792862,5792878,48966254,48966262,49472083,49472091,49498953,49498961,49623481,49623489,49643955,49643963,49769377,49769385,49822881,49822889,49833482,49833490,49842915,49842923,49924726,49924734,49943079,49943087,49979618,49979626,50266062,50266070,50273568,50273576,50297096,50297104,50439140,50439148,50513174,50513182,50561403,50561411,50562718,50562726,50587042,50587050,50596294,50596302,71079818,71079826,71085221,71085229,71121008,71121016,71145291,71145299,71330054,71330062,71387252,71387260,71387949,71387957,71407145,71407161,71466047,71466055,71528557,71528565,71530183,71530191,71544754,71544762,71546245,71546253,71652641,71658000,71658008,71659913,71659921,71679560,71679568,71690000,71690008,71710100,71710108,71722078,71722094,71825343,71825351,71855050,71855058,71897987,71897995,71960360,71960368,94327691,94327699,94353308,94353316,94386917,94386925,94434457,94434465,94507599,94507615,94517155,94517171,94586079,94586087,94629717,94629725,94692478,94692486,94785209,94785217,94818995,94819000,94819459,94819464,94904109,94904117,94919052,94919060,94940234,95087066,95087074,95104239,95104247,95112045,95112053,95112793,95112801,95129666,95182361,95182367,95194601,95194617,95250121,95250137,95288980,95288996,95314642,95314650,99251923,99251931,99368692,99368700,99402391,99402399,99440913,99440921,99457747,99457755,101439789,101439794,101439961,101439966,101440072,101440077,101448275,101448280,101489579,101489584,101489869,101489874,101492871,101492879,101519300,101519308,101562426,101562434,101575563,101575568,101631291,101631299,101672653,101672658,101686997,101687005,101755272,101755277,101755535,101755540,101823163,101823171,101858250,101858255,101860647,101860655,101887654,101887662,101889083,101889091,101917305,101917313,101933571,101933579,102030562,102030570,102059561,102059569,102070736,102070744,102070950,102070958,102146507,102146515,102161527,102161535,102178322,102178330,102198382,102198390,102236186,102236194,102244529,102244537,102280648,102280656,102287444,102287452,102343500,102343508,102402739,102402747,102459948,102459956,102467185,102467190,102469720,102469728,102531763,102531768,102548681,102548689,102673495,102721149,102721154,102762235,102762243,102783589,102783597,102787603,102807810,102807818,102830990,102830995,102867935,102867943,102883853,102883861,102902828,102902836,102903560,102903568,102925879,102925887,102926563,102926571,102932627,102932635,102956625,102956633,102986907,102986915,103011539,103011547,103062246,103062254,103132928,103163064,103163072,103175870,103175878,103276152,103285826,103285834,103286484,103286492,103288386,103288394,103289116,103289121,103289234,103289239,103326467,103326475,103343790,103343798,104573979,104573987,104575423,104575428,104575452,104575457,104575717,104575722,104575959,104575964,104601638,104615695,104615703,104627936,104627942,104646626,104646631,104699544,104699552,104758759,104758764,104761811,104761819,104794543,104794551,104804609,104804614,104842283,104842288,104907891,104907899,104921549,104921554,104963382,104963390,104976597,104976605,104983129,104983137,105082077,105082085,105083808,105083816,105085049,105085057,105087668,105087676,105125297,105125305,105149071,105149076,105180934,105180942,105196917,105198358,105198363,105255618,105255626,105283787,105283795,105292856,105292864,105345084,105345089,105346104,105346112,105360398,105360406,105373844,105373852,105393791,105393799,105439311,105439319,115523435,115523440,115595109,115601089,115601097,115602096,115602101,115624489,115624494,115626499,115626507,115665048,115665053,115665254,115665259,115669398,115669406,115702575,115702580,115704447,115704452,115720067,115720072,115720079,115720084,115720091,115720096,115779921,115779929,115780338,115780346,115797529,115797534,115821367,115821375,115849188,115849193,115853403,115853408,115854848,115854856,115909901,115909909,115916250,115916255,115955832,115955837,115978406,115978411,116062630,116062638,116068798,116068806,116076925,116076931,116091118,116176749,116176757,116221374,116221382,116250765,116250773,116263257,116263262,116276361,116276369,116316526,116372577,116372585,116400426,116400434,116415974,116415982,116479751,116480001,116502867,116508642,116508647,116514183,116514189,116538456,116538464,116609591,116609599,116692554,116692559,116699449,116699457,116731449,116731455,116731463,116731469,116731477,116731483,116731491,116731497,116731505,116731511,116731519,116731525,116757004,116757012,116780649,116780655,116780663,116780669,116780677,116780683,116780691,116780697,116780705,116780711,116840694,116840699,116901242,116901250,116901323,116901331,116902207,116902215,116907390,116907398,116908937,116908945,116909110,116909118,116909251,116909259,116913348,116924358,116924363,116929313,116929321,116929895,116929903,116930096,116930104,116980387,116980395,116980925,116980933,116981347,116981355,116981488,116981496,116981889,116981897,116982412,116982420,116982733,116982741,116982834,116982842,116983215,116983223,116983256,116983264,116984688,116984696,116986250,116986258,116988264,116988272,117000852,117000860,117011013,117011021,117040874,117040879,117056460,117056465,117072268,117072273,117148946,117148952,117205587,117205595,117224159,117224167,117250826,117250831,117268997,117269005,117286871,117286879,117290270,117290278,117297869,117297874,117309314,117309322,117322450,117419463,117419471,117457818,117457826,117474409,117474414,117503037,117503045,117504283,117504291,117547601,117547609,117570900,117586569,117586574,117668232,117668240,117852699,117852704,117879413,117879418,117895880,117895888,117969965,117969970,117983432,117983437,118029473,118029479,118057575,118057581,118098209,118098217,118110594,118110602,118127412,118127417,118131981,118131989,118145052,118145060,118155108,118155115,118171101,118181389,118181395,118199432,118199438,118201633,118201649,118213586,118213591,118237075,118237083,118240038,118240044,118281084,118281092,118285335,118291856,118291864,118292262,118292268,118292276,118292282,118311122,118311127,118321750,118321755,118367897,118367905,118368876,118368884,118417404,118417412,118419982,118419988,118484739,118484747,118497529,118497535,118531449,118541625,118556067,118556075,118563498,118566900,118567254,118567262,118567305,118567312,118575738,118575744,118579779,118579784,118596018,118596023,118599441,118599449,118627669,118627677,118656880,118656885,118679281,118693879,118693884,118711089,118711094,118781276,118781281,118795502,118795508,118824720,118824725,118938363,118938369,118952789,118952797,118960928,118960936,118965149,118965154,119022292,119022300,119058017,119058022,119096381,119096389,119109614,119109622,119120930,119120935,119140504,119140512,119170344,119170349,119194865,119194870,119202378,119202386,119207504,119207509,119208339,119208347,119235564,119235569,119237704,119237709,119287095,119287101,119311110,119311115,119346525,119347014,119350203,119350208,119367370,119367375,119418480,119418485,119419025,119419030,119419128,119419133,119464287,119464292,119474794,119474802,119525128,119525133,119528422,119528430,119530160,119530168,119538285,119538290,119538769,119538776,119546473,119546479,119546481,119546486,119566008,119566016,119577710,119577718,119595087,119595095,119597434,119597439,119598473,119598478,119605210,119605218,119608194,119608200,119612419,119612423,119614697,119614703,119615505,119615510,119617725,119621970,119667252,119667260,119671650,119671658,119680681,119680688,119685915,119685921,119702629,119702637,119770525,119770533,119771871,119771879,119799515,119799521,119804144,119804152,119826320,119826325,119844371,119844376,119854007,119854012,119865996,119866004,119870060,119870068,119873082,119873087,119909130,119909138,119914542,119914547,119919341,119919349,119941541,119941549,119967869,119967877,120008596,120008601,120008654,120008659,120025498,120025503,120048971,120048979,120074338,120074344,120091095,120091101,120122552,120122557,120125479,120125484,120156652,120156657,120161430,120161435,120163559,120163564,120164825,120164830,120169620,120169628,120175727,120175735,120182621,120182626,120190042,120190047,120191501,120191506,120222179,120250425,120250430,120251570,120251575,120257682,120257690,120272513,120275434,120275439,120286666,120286671,120300739,120300745,120336736,120380560,120380565,120380586,120380591,120392419,120403603,120403609,120407815,120408009,120408014,120418103,120418108,120426040,120426046,120426054,120426060,120426082,120426088,120426096,120426102,120426110,120426116,120465737,120465742,120493212,120493220,120533751,120533756,120603282,120603287,120612086,120612091,120626049,120626054,120644082,120644090,120646512,120647267,120647272,120650503,120650511,120664787,120664795,120690287,120690294,120700407,120700415,120711524,120711531,120714155,120715203,120715208,120716766,120716771,120730864,120730872,120731452,120731459,120770717,120770722,120775006,120775013,120777465,120777470,120797237,120797245,120797277,120797284,120815185,120815190,120846899,120846904,120887325,120887330,120900392,120900400,120912888,120912896,120917333,120917339,120917742,120917748,120917774,120917780,120921655,1…</script>
            # or:
            # <script nonce="">window.viewerData = {config: {'id': '10pYur59yGoU42E1D13YU_rbYModCl9xl', 'title': 'name2.pdf', 'isItemTrashed':  false , 'documentResourceKey': '','disableTexmexShareClient': true,}, configJson: ["0",null,null,null,null,1,null,null,null,null,1,[1,"https://clients6.google.com",null,"AIzaSyDVQw45DwoYh632gvsP5vPDqEKvb-Ywnb8",1,null,null,null,null,"AIzaSyC1eQ1xj69IdTMeii5r7brs3R90eck-m7k",null,"/drive/v2internal",1,1,null,null,null,null,"/drive/v2internal",1],null,null,null,"https://drive.google.com",null,null,"https://drive.google.com",null,null,null,null,null,null,null,null,null,null,1,null,null,[["20261029.00_p2"],6,1,1,null,null,null,1,["ADFN-ctseFmXU33pUFl_wNqrfVdVsnWUCbNoWr5R1FDjbDsumL0CXHLr0wOZTbpSqYMczzWb1NSg",1,"CKH-i7eL_pcDFfXAcAIds_8rhg",1794337325383456,[5703839,5704621,5704693,5704745,5705891,5706786,5713195,5792862,5792878,48966254,48966262,49472083,49472091,49498953,49498961,49623481,49623489,49643955,49643963,49769377,49769385,49822881,49822889,49833482,49833490,49842915,49842923,49924726,49924734,49943079,49943087,49979618,49979626,50266062,50266070,50273568,50273576,50297096,50297104,50439140,50439148,50513174,50513182,50561403,50561411,50562718,50562726,50587042,50587050,50596294,50596302,71079818,71079826,71085221,71085229,71121008,71121016,71145291,71145299,71330054,71330062,71387252,71387260,71387949,71387957,71407145,71407161,71466047,71466055,71528557,71528565,71530183,71530191,71544754,71544762,71546245,71546253,71652641,71658000,71658008,71659913,71659921,71679560,71679568,71690000,71690008,71710100,71710108,71722078,71722094,71825343,71825351,71855050,71855058,71897987,71897995,71960360,71960368,94327691,94327699,94353308,94353316,94386917,94386925,94434457,94434465,94507599,94507615,94517155,94517171,94586079,94586087,94619289,94619297,94629717,94629725,94692478,94692486,94785209,94785217,94818995,94819000,94819459,94819464,94904109,94904117,94919052,94919060,94940234,95087066,95087074,95104239,95104247,95112045,95112053,95112793,95112801,95129666,95182361,95182367,95194601,95194617,95250121,95250137,95288980,95288996,95314642,95314650,99251923,99251931,99368692,99368700,99402391,99402399,99440913,99440921,99457747,99457755,101439789,101439794,101439961,101439966,101440072,101440077,101448275,101448280,101489579,101489584,101489869,101489874,101492871,101492879,101519300,101519308,101562426,101562434,101575563,101575568,101631291,101631299,101672653,101672658,101686997,101687005,101755272,101755277,101755535,101755540,101823163,101823171,101858250,101858255,101860647,101860655,101887654,101887662,101889083,101889091,101917305,101917313,101933571,101933579,102030562,102030570,102059561,102059569,102070736,102070744,102070950,102070958,102146507,102146515,102161527,102161535,102178322,102178330,102198382,102198390,102236186,102236194,102244529,102244537,102280648,102280656,102287444,102287452,102343500,102343508,102402739,102402747,102459948,102459956,102467185,102467190,102469720,102469728,102531763,102531768,102548681,102548689,102673495,102721149,102721154,102762235,102762243,102783589,102783597,102787603,102807810,102807818,102830990,102830995,102867935,102867943,102883853,102883861,102902828,102902836,102903560,102903568,102925879,102925887,102926563,102926571,102932627,102932635,102956625,102956633,102986907,102986915,103011539,103011547,103062246,103062254,103132928,103163064,103163072,103175870,103175878,103276152,103285826,103285834,103286484,103286492,103288386,103288394,103289116,103289121,103289234,103289239,103326467,103326475,103343790,103343798,104573979,104573987,104575423,104575428,104575452,104575457,104575717,104575722,104575959,104575964,104601638,104615695,104615703,104627936,104627942,104646626,104646631,104699544,104699552,104758759,104758764,104761811,104761819,104794543,104794551,104804609,104804614,104842283,104842288,104907891,104907899,104921549,104921554,104963382,104963390,104976597,104976605,104983129,104983137,105082077,105082085,105083808,105083816,105085049,105085057,105087668,105087676,105125297,105125305,105149071,105149076,105180934,105180942,105196917,105198358,105198363,105255618,105255626,105283787,105283795,105292856,105292864,105345084,105345089,105346104,105346112,105360398,105360406,105373844,105373852,105393791,105393799,105439311,105439319,115523435,115523440,115595109,115601089,115601097,115602096,115602101,115624489,115624494,115626499,115626507,115665048,115665053,115665254,115665259,115669398,115669406,115702575,115702580,115704447,115704452,115720067,115720072,115720079,115720084,115720091,115720096,115779921,115779929,115780338,115780346,115797529,115797534,115821367,115821375,115849188,115849193,115853403,115853408,115854848,115854856,115909901,115909909,115916250,115916255,115955832,115955837,115978406,115978411,116062630,116062638,116068798,116068806,116076925,116076931,116091118,116176749,116176757,116221374,116221382,116250765,116250773,116263257,116263262,116276361,116276369,116316526,116372577,116372585,116400426,116400434,116415974,116415982,116479751,116480001,116502867,116508642,116508647,116514183,116514189,116538456,116538464,116609591,116609599,116692554,116692559,116699449,116699457,116731449,116731455,116731463,116731469,116731477,116731483,116731491,116731497,116731505,116731511,116731519,116731525,116757004,116757012,116780649,116780655,116780663,116780669,116780677,116780683,116780691,116780697,116780705,116780711,116840694,116840699,116901242,116901250,116901323,116901331,116902207,116902215,116907390,116907398,116908937,116908945,116909110,116909118,116909251,116909259,116913348,116924358,116924363,116929313,116929321,116929895,116929903,116930096,116930104,116980387,116980395,116980925,116980933,116981347,116981355,116981488,116981496,116981889,116981897,116982412,116982420,116982733,116982741,116982834,116982842,116983215,116983223,116983256,116983264,116984688,116984696,116986250,116986258,116988264,116988272,117000852,117000860,117011013,117011021,117040874,117040879,117056460,117056465,117072268,117072273,117148946,117148952,117205587,117205595,117224159,117224167,117250826,117250831,117268997,117269005,117286871,117286879,117290270,117290278,117297869,117297874,117309314,117309322,117322450,117419463,117419471,117457818,117457826,117474409,117474414,117503037,117503045,117504283,117504291,117547601,117547609,117570900,117586569,117586574,117668232,117668240,117852699,117852704,117879413,117879418,117895880,117895888,117969965,117969970,117983432,117983437,118029473,118029479,118057575,118057581,118098209,118098217,118110594,118110602,118127412,118127417,118131981,118131989,118145052,118145060,118155108,118155115,118171101,118181389,118181395,118199432,118199438,118201633,118201649,118213586,118213591,118237075,118237083,118240038,118240044,118281084,118281092,118285335,118291856,118291864,118292262,118292268,118292276,118292282,118311122,118311127,118321750,118321755,118367897,118367905,118368876,118368884,118417404,118417412,118419982,118419988,118484739,118484747,118497529,118497535,118531449,118541625,118556067,118556075,118563498,118566900,118567254,118567262,118567305,118567312,118575738,118575744,118579779,118579784,118596018,118596023,118599441,118599449,118627669,118627677,118656880,118656885,118679281,118693879,118693884,118711089,118711094,118781276,118781281,118795502,118795508,118824720,118824725,118938363,118938369,118952789,118952797,118960928,118960936,118965149,118965154,119022292,119022300,119058017,119058022,119096381,119096389,119109614,119109622,119120930,119120935,119140504,119140512,119170344,119170349,119194865,119194870,119202378,119202386,119207504,119207509,119208339,119208347,119235564,119235569,119237704,119237709,119287095,119287101,119311110,119311115,119346525,119347014,119350203,119350208,119367370,119367375,119418480,119418485,119419025,119419030,119419128,119419133,119464287,119464292,119474794,119474802,119525128,119525133,119528422,119528430,119530160,119530168,119538285,119538290,119538769,119538776,119546473,119546479,119546481,119546486,119566008,119566016,119577710,119577718,119595087,119595095,119597434,119597439,119598473,119598478,119605210,119605218,119608194,119608200,119612419,119612423,119614697,119614703,119615505,119615510,119617725,119621970,119667252,119667260,119671650,119671658,119680681,119680688,119685915,119685921,119702629,119702637,119770525,119770533,119771871,119771879,119799515,119799521,119804144,119804152,119826320,119826325,119844371,119844376,119854007,119854012,119865996,119866004,119870060,119870068,119873082,119873087,119909130,119909138,119914542,119914547,119919341,119919349,119941541,119941549,119967869,119967877,120008596,120008601,120008654,120008659,120025498,120025503,120048971,120048979,120074338,120074344,120091095,120091101,120122552,120122557,120125479,120125484,120156652,120156657,120161430,120161435,120163559,120163564,120164825,120164830,120169620,120169628,120175727,120175735,120182621,120182626,120190042,120190047,120191501,120191506,120222179,120250425,120250430,120251570,120251575,120257682,120257690,120272513,120275434,120275439,120286666,120286671,120300739,120300745,120336736,120380560,120380565,120380586,120380591,120392419,120403603,120403609,120407815,120408009,120408014,120418103,120418108,120426040,120426046,120426054,120426060,120426082,120426088,120426096,120426102,120426110,120426116,120465737,120465742,120493212,120493220,120533751,120533756,120603282,120603287,120612086,120612091,120626049,120626054,120644082,120644090,120646512,120647267,120647272,120650503,120650511,120664787,120664795,120690287,120690294,120700407,120700415,120711524,120711531,120714155,120715203,120715208,120716766,120716771,120730864,120730872,120731452,120731459,120770717,120770722,120775006,120775013,120777465,120777470,120797237,120797245,120797277,120797284,120815185,120815190,120846899,120846904,120887325,120887330,120900392,120900400,120912888,120912896,120917333,120917339,120917742,120917748,120917774,120917780,120921655,1209329…</script>
            # you can look at the 'title' and extract info from it
            # you can also use this method as a fallback way to extract fileID in the function __get_fileID()
            # or in function get_file_name() to extract the exact name

            # if 'folder' appears in the url, its document type is folder
            if "folder" in (page.url or ""):
                return DocumentType.FOLDER

            title = None
            try:
                title = await page.evaluate("() => window.viewerData?.config?.title")
            except Exception:
                pass

            if not title:
                html = await page.content()
                match = re.search(
                    r"""window\.viewerData.*?['"]title['"]\s*:\s*['"]([^'"]+)['"]""",
                    html,
                    re.DOTALL,
                )
                if match:
                    title = match.group(1)

            if title and "." in title:
                ext = title.rsplit(".", 1)[-1].lower()
                ext_map = {
                    "pdf": DocumentType.PDF,
                    "pptx": DocumentType.PPTX,
                    "ppt": DocumentType.PPTX,
                    "doc": DocumentType.MICROSOFT_WORD,
                    "docx": DocumentType.MICROSOFT_WORD,
                    "xlsx": DocumentType.XLSX,
                    "xls": DocumentType.XLSX,
                    "png": DocumentType.PNG,
                    "jpg": DocumentType.JPG,
                    "jpeg": DocumentType.JPEG,
                    "zip": DocumentType.ZIP_FOLDER,
                }
                if ext in ext_map:
                    return ext_map[ext]
                """
                if it can't determine the document type, do this final check on the logo:
                if the link is "https://drive.google.com/file/d/{file_id}/view"
                the template body will have exactly this one element:
                <svg height="24" width="24" viewBox="3 3 18 18" fill="none" xmlns="http://www.w3.org/2000/svg" class="yLKXEe Q6yead L1hHpb" mask-id="ucc-40"><g><rect x="3" y="3" width="18" height="18" rx="3" fill="#3186FF"></rect><rect x="7" y="15" width="6" height="2" rx="1" fill="white"></rect><rect x="7" y="11" width="10" height="2" rx="1" fill="white"></rect></g></svg>
                if this element present in the template body
                then the document_type is DocumentType.GOOGLE_DOC
                """

            if "/file/" in urlparse(page.url).path:
                logo = page.locator(
                    'svg[height="24"][width="24"][viewBox="3 3 18 18"]'
                    '[fill="none"][xmlns="http://www.w3.org/2000/svg"]'
                    ':has(> g > rect[x="3"][y="3"][width="18"][height="18"]'
                    '[rx="3"][fill="#3186FF"])'
                    ':has(> g > rect[x="7"][y="15"][width="6"][height="2"]'
                    '[rx="1"][fill="white"])'
                    ':has(> g > rect[x="7"][y="11"][width="10"][height="2"]'
                    '[rx="1"][fill="white"])'
                )
                try:
                    await logo.first.wait_for(state="attached", timeout=1_500)
                    return DocumentType.GOOGLE_DOC
                except Exception:
                    pass

            raise ValueError(
                f"Unable to determine Google Drive document type (title: {title!r}, url: {page.url})"
            )
        elif self.website_type == WebsiteType.GOOGLE_DOC:
            # if page.url domain is 'docs.google.com'
            # check for the next path to determine whether its is doc or xlsx
            # for example:
            # if the url is: "https://docs.google.com/spreadsheets/d/{ID}/view" --> xlsx format
            # if the url is: "https://docs.google.com/document/d/{ID}/view" --> doc format
            path = urlparse(page.url).path.lower()
            if "/spreadsheets/" in path:
                return DocumentType.XLSX
            elif "/document/" in path:
                parser = _TitleParser()
                parser.feed(await page.content())
                title = "".join(parser.title_parts).strip()
                if ".docx" in title.lower():
                    return DocumentType.MICROSOFT_WORD
                return DocumentType.GOOGLE_DOC
            elif "/presentation/" in path:
                return DocumentType.PPTX
            raise ValueError(
                f"Unable to determine Google Docs document type from path: {path!r} (url: {page.url})"
            )
        elif self.website_type == WebsiteType.SCRIBD:
            return DocumentType.PDF  # always

        raise ValueError(
            f"Unable to determine document type for website {self.website_type} (url: {page.url})"
        )

    async def __check_for_download_availability(self, page: Page) -> bool:
        if self.website_type in (WebsiteType.GOOGLE_DRIVE, WebsiteType.GOOGLE_DOC):
            # do check for the pdf template
            if self.document_type in (DocumentType.PDF, DocumentType.PPTX):
                # in the html template, there is exactly one <span> element that contain the text 'Ctrl+D'
                # for example:
                # <span class="aqdrmf-rymPhb-JMEf7e" jsname="orbTae">Ctrl+D</span>
                # if the current template has that element -> the document is Downloadable -> return True
                # else: return false
                try:
                    locator = page.locator(
                        'span[jsname="orbTae"]:has-text("Ctrl+D"), span:has-text("Ctrl+D")'
                    )
                    await locator.first.wait_for(state="attached", timeout=1500)
                    if await locator.count() > 0:
                        return True
                except Exception:
                    pass
                html = await page.content()
                return "Ctrl+D" in html and (
                    "orbTae" in html or "aqdrmf-rymPhb-JMEf7e" in html
                )

            if self.document_type == DocumentType.MICROSOFT_WORD:
                # in google doc, and in the main <head> element there is an element look like this
                # <script nonce="">var DOCS_warmStartDocumentLoader; var DOCS_cseBlobArtifactsHolder; var DOCS_initialLoadTiming; var _docs_annotate_getAnnotatedText; function KX_startKix() {var config = {'bei': '\x5b\x5d', 'bl': 'null', 'clt':  0.0 , 'docid': '15mUH39Zw_zsABKGTIWrf__eOx_4TOUgoRAyEkhFzbXE', 'downloadable':  true , 'ear':  true , 'evmp':  true , 'ebc':  false , 'ecso':  true , 'edo':  true , 'edr':  true , 'eex':  true , 'eimg':  true , 'elementId': 'docs-editor', 'els':  false , 'email': 'thanhphatggclass@gmail.com', 'ende':  false , 'eoo':  true , 'epff':  true , 'epub':  true , 'erv':  true , 'esvcm':  true , 'etr':  true , 'flr':  0.0 , 'fpu': 'https:\/\/docs.google.com\/picker?protocol\x3dgadgets\x26parent\x3dhttps:\/\/docs.google.com\/relay.html\x26hostId\x3dkix-fonts\x26title\x3dPh%C3%B4ng+ch%C6%B0%CC%83\x26hl\x3dvi\x26authuser\x3d0\x26newDriveView\x3dtrue\x26origin\x3dhttps:\/\/docs.google.com\x26navHidden\x3dtrue\x26multiselectEnabled\x3dtrue\x26selectButtonLabel\x3dOK\x26nav\x3d((%22fonts%22))', 'dsii':  true , 'ingc':  false , 'idu':  null , 'ihm':  false , 'isd':  false , 'ist':  false , 'l': 'vi', 'ogi': '02183189086966558456', 'oui': '02183189086966558456', 'fogi': '109015353153985414909', 'psi': 'kix-pp', 'rtl':  false , 'up': '\/document\/d\/15mUH39Zw_zsABKGTIWrf__eOx_4TOUgoRAyEkhFzbXE', 'un': 'thanhphatggclass', 'wfff': _docs_webfonts_fontFaces, 'crt':  1.7240544E12 , 'themeMode':  0.0 ,};config['bci'] = '15mUH39Zw_zsABKGTIWrf__eOx_4TOUgoRAyEkhFzbXE';config['dct'] =  1.792979589931E12 ;config['epl'] = true;config['erts'] = true;config['er'] = true;config['hudc'] =  true ;config['lvr'] =  747.0 ;config['lvts'] =  1.794158617501E12 ;config['lvrat'] = 'Sr7PInfzaZ8rmQ';config['cslvmr'] =  28000.0 ;config['ddi'] = [];config['cseft'] =  0.0 ;config['eds'] =  1.0 ;config['cswu'] = 'https:\/\/docs.google.com\/static\/document\/client\/wasm\/4285522420-gspell.wasm';config['csmu'] = {"de":"https://docs.google.com/static/document/client/wasm/4005965585-de.model","en":"https://docs.google.com/static/document/client/wasm/174533592-en.model","en_GB":"https://docs.google.com/static/document/client/wasm/672482371-en_GB.model","es":"https://docs.google.com/static/document/client/wasm/2405813201-es.model","fr":"https://docs.google.com/static/document/client/wasm/2662797112-fr.model","it":"https://docs.google.com/static/document/client/wasm/1169698086-it.model"};config['csfu'] = {"de":"https://docs.google.com/static/document/client/wasm/3383622092-de_hashed_fairness.binarypb","en":"https://docs.google.com/static/document/client/wasm/1092881629-en_hashed_fairness.binarypb","en_GB":"https://docs.google.com/static/document/client/wasm/1092881629-en_GB_hashed_fairness.binarypb","es":"https://docs.google.com/static/document/client/wasm/3631108950-es_hashed_fairness.binarypb","fr":"https://docs.google.com/static/document/client/wasm/167824407-fr_hashed_fairness.binarypb","it":"https://docs.google.com/static/document/client/wasm/2409494728-it_hashed_fairness.binarypb"};config['qs'] =  1.0 ;config['nrfd'] =  false ;var KX_kixApp = _createKixApplication('1996afc1c2773305', config, 4.0 ,'\x5b\x22docs.security.access_capabilities\x22,1,1,1,1,1,0,0,1,1,1,1,1,1,1,1,0,0,1,0,0,0,0,1,0,1,null,1,0\x5d','Lab5', 'Tài liệu không có tiêu đề', '', false , {"s-blob-v1-IMAGE-4OkHbVDDMz8":"https://docs.google.com/u/0/docs-images-rt/AIpPl2CYhddpUYCKIJebKEOA8fA7v-noi0jJMh8UAUfyxiUmC0yibXFn5YFvvbEJNhkPIl5fHnaEs8cPwo5AEzoQm_-BAQ5QHNrwk11ZAi3E_gAy_RpmtvDr7Lc-yAk5KgxLWiHiyugXAvWEwc30B0aQh_4htYS-bNbSWFFbFzYyXQ\u003ds2048","s-blob-v1-IMAGE-RJDrSKY1nQ0":"https://docs.google.com/u/0/docs-images-rt/AIpPl2AlwIzdrIt4Xz4iyKLK6EB_NIXAOo4FnkEsdc40eeNnaU-pdT_x4TtxVLyut_4lThSF0LsC6wnSRNiL5mFpnzwNPXA4XBrTwIc4Cx1v-BWkcF7ceUVEKN9lTMrgzIdkweyGKjMXVIDPeFo99zUV9a-0zrqEGgznATBcoHugVQ\u003ds2048","s-blob-v1-IMAGE-6LBL87nkqhE":"https://docs.google.com/u/0/docs-images-rt/AIpPl2AYOQcaKbxlmHfOe8vQ-4ADs3rSslf6GRZUZWQaT3-DFmuSM7iY6SCw2G6GucdPoE0eIfAhkWyGcEBFH4349z_rAMcQpjV6BgKzZ7zE0Itx4gBo456GNDemwsQtrnFuPcDNjrEl-IQTUvQW4KN2xAu-8ovexkEBrg9MK7_ZKw\u003ds2048","s-blob-v1-IMAGE-pcg6yygXuj8":"https://docs.google.com/u/0/docs-images-rt/AIpPl2BuA-dl54q5MuXmxv03B1fLrM0UI26Up2EX36GtuBoHk6PSmG0k1tvw3QK0kKfbgFhNZrSAbfYuvibXABulrVZypCzXgXLnmVsl7yxAcdSKQ9-2GZ_dwUQ6xCmxEx71uEPTzb74Xs1UmxMjXbV_SOMYUMzDTFfMgaWyWR7bzw\u003ds2048","s-blob-v1-IMAGE-bue7Apug75A":"https://docs.google.com/u/0/docs-images-rt/AIpPl2AVJCvhu6aQMXbjSfWfaER-aSSluedFqyauObGbnZ2bGEOCCTV4eR_C9FqHHFAmadQ4xUZAKxvAOvzQh_UeNxnZQnG23j-mH13xQBGB5sdWwki4pEbRrT1vqwR8ANjfJkPph_VwnJJ3v6DJ7FMoFQgT1qPbdQO8EK1qOe4\u003ds2048","s-blob-v1-IMAGE-EiP4KLkvaY4":"https://docs.google.com/u/0/docs-images-rt/AIpPl2CHWGQZbofZyqZpnBMcmJyexPPpr6eLbkCT7Z-f1_r2BF_bCeZBpDP0GpTDzs94JGy5QQ6qdf4yH43SK7GSIqv3IJX3U2BKf6xlTgbk2u3loibPl6kcnrRr59rhgvABP4xQ3cNzaUNMFSx0RQt9Xzp2PDR93AdPp9C9S4ullg\u003ds2048","s-blob-v1-IMAGE-kohBNGB9MWk":"https://docs.google.com/u/0/docs-images-rt/AIpPl2A0aIuLGo3c4PtvKkBt2RrXyvHNG3dMHtA5KJSxJfZ-GakGq3uc1-fDBsq6-Xj8kKgivijytikwenm9SKzVCIGx-MLFM0BUdCSozO3bxDMQiBXhe8jJ4rh7fmmT_hKbZkecAZP8VT4D_nyxblTz7KDu4tVTGW4Ntx7l7vshyg\u003ds2048"} , undefined , {} , {"":[false,false],"t.b2jhhcz3fx7d":[false,false],"t.sdbgcrq1p5xc":[false,false],"t.yeas5sxwdfkz":[false,false],"t.194l8fg4zoth":[false,false]}, false , {"":false,"t.b2jhhcz3fx7d":false,"t.sdbgcrq1p5xc":false,"t.yeas5sxwdfkz":false,"t.194l8fg4zoth":false} , false , {"":false,"t.b2jhhcz3fx7d":false,"t.sdbgcrq1p5xc":false,"t.yeas5sxwdfkz":false,"t.194l8fg4zoth":false} , {"hs_h3":{"sdef_ps":{"ps_awao_i":true,"ps_klt_i":false,"ps_ir":0.0,"ps_ls_i":true,"ps_al_i":true,"ps_ltr":true,"ps_rd":"","ps_bl_i":true,"ps_sd_i":true,"ps_sb_i":false,"ps_ifl":0.0,"ps_sa_i":false,"ps_shd":false,"ps_pbb":false,"ps_br_i":true,"ps_klt":true,"ps_bt_i":true,"ps_hd":0,"ps_kwn_i":false,"ps_ifl_i":true,"ps_ts":{"cv":{"op":"set","opValue":[]}},"ps_bbtw":null,"ps_lslm":0,"ps_bb":null,"ps_ls":1.15,"ps_bl":null,"ps_kwn":true,"ps_sm_i":true,"ps_il_i":true,"ps_sa":4.0,"ps_sb":16.0,"ps_br":null,"ps_ir_i":true,"ps_sd":null,"ps_bt":null,"ps_hdid":"","ps_lslm_i":true,"ps_sm":0,"ps_bbtw_i":true,"ps_al":0,"ps_bb_i":true,"ps_pbb_i":false,"ps_awao":false,"ps_il":0.0},"sdef_ts":{"ts_un":false,"ts_un_i":true,"ts_sc":false,"ts_it_i":true,"ts_fgc2":{"hclr_color":"#434343","clr_type":0},"ts_lsp_i":false,"ts_st_i":true,"ts_bgc2":{"hclr_color":null,"clr_type":0},"ts_fs_i":false,"ts_ff_i":true,"ts_it":false,"ts_va":"nor","ts_bgc2_i":true,"ts_bd_i":false,"ts_va_i":true,"ts_fs":14.0,"ts_ff":"Arial","ts_st":false,"ts_bd":false,"ts_sc_i":true,"ts_fgc2_i":false,"ts_lsp":{"lspp_points":0.0,"lsp_type":0},"ts_tw":400}},"hs_t":{"sdef_ps":{"ps_awao_i":true,"ps_klt_i":false,"ps_ir":0.0,"ps_ls_i":true,"ps_al_i":true,"ps_ltr":true,"ps_rd":"","ps_bl_i":true,"ps_sd_i":true,"ps_sb_i":false,"ps_ifl":0.0,"ps_sa_i":false,"ps_shd":false,"ps_pbb":false,"ps_br_i":true,"ps_klt":true,"ps_bt_i":true,"ps_hd":0,"ps_kwn_i":false,"ps_ifl_i":true,"ps_ts":{"cv":{"op":"set","opValue":[]}},"ps_bbtw":null,"ps_lslm":0,"ps_bb":null,"ps_ls":1.15,"ps_bl":null,"ps_kwn":true,"ps_sm_i":true,"ps_il_i":true,"ps_sa":3.0,"ps_sb":0.0,"ps_br":null,"ps_ir_i":true,"ps_sd":null,"ps_bt":null,"ps_hdid":"","ps_lslm_i":true,"ps_sm":0,"ps_bbtw_i":true,"ps_al":0,"ps_bb_i":true,"ps_pbb_i":false,"ps_awao":false,"ps_il":0.0},"sdef_ts":{"ts_un":false,"ts_un_i":true,"ts_sc":false,"ts_it_i":true,"ts_fgc2":{"hclr_color":"#000000","clr_type":0},"ts_lsp_i":false,"ts_st_i":true,"ts_bgc2":{"hclr_color":null,"clr_type":0},"ts_fs_i":false,"ts_ff_i":true,"ts_it":false,"ts_va":"nor","ts_bgc2_i":true,"ts_bd_i":true,"ts_va_i":true,"ts_fs":26.0,"ts_ff":"Arial","ts_st":false,"ts_bd":false,"ts_sc_i":true,"ts_fgc2_i":true,"ts_lsp":{"lspp_points":0.0,"lsp_type":0},"ts_tw":400}},"hs_h2":{"sdef_ps":{"ps_awao_i":true,"ps_klt_i":false,"ps_ir":0.0,"ps_ls_i":true,"ps_al_i":true,"ps_ltr":true,"ps_rd":"","ps_bl_i":true,"ps_sd_i":true,"ps_sb_i":false,"ps_ifl":0.0,"ps_sa_i":false,"ps_shd":false,"ps_pbb":false,"ps_br_i":true,"ps_klt":true,"ps_bt_i":true,"ps_hd":0,"ps_kwn_i":false,"ps_ifl_i":true,"ps_ts":{"cv":{"op":"set","opValue":[]}},"ps_bbtw":null,"ps_lslm":0,"ps_bb":null,"ps_ls":1.15,"ps_bl":null,"ps_kwn":true,"ps_sm_i":true,"ps_il_i":true,"ps_sa":6.0,"ps_sb":18.0,"ps_br":null,"ps_ir_i":true,"ps_sd":null,"ps_bt":null,"ps_hdid":"","ps_lslm_i":true,"ps_sm":0,"ps_bbtw_i":true,"ps_al":0,"ps_bb_i":true,"ps_pbb_i":false,"ps_awao":false,"ps_il":0.0},"sdef_ts":{"ts_un":false,"ts_un_i":true,"ts_sc":false,"ts_it_i":true,"ts_fgc2":{"hclr_color":"#000000","clr_type":0},"ts_lsp_i":false,"ts_st_i":true,"ts_bgc2":{"hclr_color":null,"clr_type":0},"ts_fs_i":false,"ts_ff_i":true,"ts_it":false,"ts_va":"nor","ts_bgc2_i":true,"ts_bd_i":false,"ts_va_i":true,"ts_fs":16.0,"ts_ff":"Arial","ts_st":false,"ts_bd":false,"ts_sc_i":true,"ts_fgc2_i":true,"ts_lsp":{"lspp_points":0.0,"lsp_type":0},"ts_tw":400}},"hs_h1":{"sdef_ps":{"ps_awao_i":true,"ps_klt_i":false,"ps_ir":0.0,"ps_ls_i":true,"ps_al_i":true,"ps_ltr":true,"ps_rd":"","ps_bl_i":true,"ps_sd_i":true,"ps_sb_i":false,"ps_ifl":0.0,"ps_sa_i":false,"ps_shd":false,"ps_pbb":false,"ps_br_i":true,"ps_klt":true,"ps_bt_i":true,"ps_hd":0,"ps_kwn_i":false,"ps_ifl_i":true,"ps_ts":{"cv":{"op":"set","opValue":[]}},"ps_bbtw":null,"ps_lslm":0,"ps_bb":null,"ps_ls":1.15,"ps_bl":null,"ps_kwn":true,"ps_sm_i":true,"ps_il_i":true,"ps_sa":6.0,"ps_sb":20.0,"ps_br":null,"ps_ir_i":true,"ps_sd":null,"ps_bt":null,"ps_hdid":"","ps_lslm_i":true,"ps_sm":0,"ps_bbtw_i":true,"ps_al":0,"ps_bb_i":true,"ps_pbb_i":false,"ps_awao":false,"ps_il":0.0},"sdef_ts":{"ts_un":false,"ts_un_i":true,"ts_sc":false,"ts_it_i":true,"ts_fgc2":{"hclr_color":"#000000","clr_type":0},"ts_lsp_i":false,"ts_st_i":true,"ts_bgc2":{"hclr_color":null,"clr_type":0},"ts_fs_i":false,"ts_ff_i":true,"ts_it":false,"ts_va":"nor","ts_bgc2_i":true,"ts_bd_i":true,"ts_va_i":true,"ts_fs":20.0,"ts_ff":"Arial","ts_st":false,"ts_bd":false,"ts_sc_i":true,"ts_fgc2_i":true,"ts_lsp":{"lspp_points":0.0,"lsp_type":0},"ts_tw":400}},"hs_nt":{"sdef_ps":{"ps_awao_i":false,"ps_klt_i":false,"ps_ir":0.0,"ps_ls_i":false,"ps_al_i":false,"ps_ltr":true,"ps_rd":"","ps_bl_i":false,"ps_sd_i":false,"ps_sb_i":false,"ps_ifl":0.0,"ps_sa_i":false,"ps_shd":false,"ps_pbb":false,"ps_br_i":false,"ps_klt":false,"ps_bt_i":false,"ps_hd":0,"ps_kwn_i":false,"ps_ifl_i":false,"ps_ts":{"cv":{"op":"set","opValue":[]}},"ps_bbtw":null,"ps_lslm":1,"ps_bb":null,"ps_ls":1.15,"ps_bl":null,"ps_kwn":false,"ps_sm_i":false,"ps_il_i":false,"ps_sa":0.0,"ps_sb":0.0,"ps_br":null,"ps_ir_i":false,"ps_sd":null,"ps_bt":null,"ps_hdid":"","ps_lslm_i":false,"ps_sm":0,"ps_bbtw_i":false,"ps_al":0,"ps_bb_i":false,"ps_pbb_i":false,"ps_awao":true,"ps_il":0.0},"sdef_ts":{"ts_un":false,"ts_un_i":false,"ts_sc":false,"ts_it_i":false,"ts_fgc2":{"hclr_color":"#000000","clr_type":0},"ts_lsp_i":false,"ts_st_i":false,"ts_bgc2":{"hclr_color":null,"clr_type":0},"ts_fs_i":false,"ts_ff_i":false,"ts_it":false,"ts_va":"nor","ts_bgc2_i":false,"ts_bd_i":false,"ts_va_i":false,"ts_fs":11.0,"ts_ff":"Arial","ts_st":false,"ts_bd":false,"ts_sc_i":false,"ts_fgc2_i":false,"ts_lsp":{"lspp_points":0.0,"lsp_type":0},"ts_tw":400}},"hs_st":{"sdef_ps":{"ps_awao_i":true,"ps_klt_i":false,"ps_ir":0.0,"ps_ls_i":true,"ps_al_i":true,"ps_ltr":true,"ps_rd":"","ps_bl_i":true,"ps_sd_i":true,"ps_sb_i":false,"ps_ifl":0.0,"ps_sa_i":false,"ps_shd":false,"ps_pbb":false,"ps_br_i":true,"ps_klt":true,"ps_bt_i":true,"ps_hd":0,"ps_kwn_i":false,"ps_ifl_i":true,"ps_ts":{"cv":{"op":"set","opValue":[]}},"ps_bbtw":null,"ps_lslm":0,"ps_bb":null,"ps_ls":1.15,"ps_bl":null,"ps_kwn":true,"ps_sm_i":true,"ps_il_i":true,"ps_sa":16.0,"ps_sb":0.0,"ps_br":null,"ps_ir_i":true,"ps_sd":null,"ps_bt":null,"ps_hdid":"","ps_lslm_i":true,"ps_sm":0,"ps_bbtw_i":true,"ps_al":0,"ps_bb_i":true,"ps_pbb_i":false,"ps_awao":false,"ps_il":0.0},"sdef_ts":{"ts_un":false,"ts_un_i":true,"ts_sc":false,"ts_it_i":false,"ts_fgc2":{"hclr_color":"#666666","clr_type":0},"ts_lsp_i":false,"ts_st_i":true,"ts_bgc2":{"hclr_color":null,"clr_type":0},"ts_fs_i":false,"ts_ff_i":false,"ts_it":false,"ts_va":"nor","ts_bgc2_i":true,"ts_bd_i":true,"ts_va_i":true,"ts_fs":15.0,"ts_ff":"Arial","ts_st":false,"ts_bd":false,"ts_sc_i":true,"ts_fgc2_i":false,"ts_lsp":{"lspp_points":0.0,"lsp_type":0},"ts_tw":400}},"hs_h6":{"sdef_ps":{"ps_awao_i":true,"ps_klt_i":false,"ps_ir":0.0,"ps_ls_i":true,"ps_al_i":true,"ps_ltr":true,"ps_rd":"","ps_bl_i":true,"ps_sd_i":true,"ps_sb_i":false,"ps_ifl":0.0,"ps_sa_i":false,"ps_shd":false,"ps_pbb":false,"ps_br_i":true,"ps_klt":true,"ps_bt_i":true,"ps_hd":0,"ps_kwn_i":false,"ps_ifl_i":true,"ps_ts":{"cv":{"op":"set","opValue":[]}},"ps_bbtw":null,"ps_lslm":0,"ps_bb":null,"ps_ls":1.15,"ps_bl":null,"ps_kwn":true,"ps_sm_i":true,"ps_il_i":true,"ps_sa":4.0,"ps_sb":12.0,"ps_br":null,"ps_ir_i":true,"ps_sd":null,"ps_bt":null,"ps_hdid":"","ps_lslm_i":true,"ps_sm":0,"ps_bbtw_i":true,"ps_al":0,"ps_bb_i":true,"ps_pbb_i":false,"ps_awao":false,"ps_il":0.0},"sdef_ts":{"ts_un":false,"ts_un_i":true,"ts_sc":false,"ts_it_i":false,"ts_fgc2":{"hclr_color":"#666666","clr_type":0},"ts_lsp_i":false,"ts_st_i":true,"ts_bgc2":{"hclr_color":null,"clr_type":0},"ts_fs_i":false,"ts_ff_i":true,"ts_it":true,"ts_va":"nor","ts_bgc2_i":true,"ts_bd_i":true,"ts_va_i":true,"ts_fs":11.0,"ts_ff":"Arial","ts_st":false,"ts_bd":false,"ts_sc_i":true,"ts_fgc2_i":false,"ts_lsp":{"lspp_points":0.0,"lsp_type":0},"ts_tw":400}},"hs_h5":{"sdef_ps":{"ps_awao_i":true,"ps_klt_i":false,"ps_ir":0.0,"ps_ls_i":true,"ps_al_i":true,"ps_ltr":true,"ps_rd":"","ps_bl_i":true,"ps_sd_i":true,"ps_sb_i":false,"ps_ifl":0.0,"ps_sa_i":false,"ps_shd":false,"ps_pbb":false,"ps_br_i":true,"ps_klt":true,"ps_bt_i":true,"ps_hd":0,"ps_kwn_i":false,"ps_ifl_i":true,"ps_ts":{"cv":{"op":"set","opValue":[]}},"ps_bbtw":null,"ps_lslm":0,"ps_bb":null,"ps_ls":1.15,"ps_bl":null,"ps_kwn":true,"ps_sm_i":true,"ps_il_i":true,"ps_sa":4.0,"ps_sb":12.0,"ps_br":null,"ps_ir_i":true,"ps_sd":null,"ps_bt":null,"ps_hdid":"","ps_lslm_i":true,"ps_sm":0,"ps_bbtw_i":true,"ps_al":0,"ps_bb_i":true,"ps_pbb_i":false,"ps_awao":false,"ps_il":0.0},"sdef_ts":{"ts_un":false,"ts_un_i":true,"ts_sc":false,"ts_it_i":true,"ts_fgc2":{"hclr_color":"#666666","clr_type":0},"ts_lsp_i":false,"ts_st_i":true,"ts_bgc2":{"hclr_color":null,"clr_type":0},"ts_fs_i":false,"ts_ff_i":true,"ts_it":false,"ts_va":"nor","ts_bgc2_i":true,"ts_bd_i":true,"ts_va_i":true,"ts_fs":11.0,"ts_ff":"Arial","ts_st":false,"ts_bd":false,"ts_sc_i":true,"ts_fgc2_i":false,"ts_lsp":{"lspp_points":0.0,"lsp_type":0},"ts_tw":400}},"hs_h4":{"sdef_ps":{"ps_awao_i":true,"ps_klt_i":false,"ps_ir":0.0,"ps_ls_i":true,"ps_al_i":true,"ps_ltr":true,"ps_rd":"","ps_bl_i":true,"ps_sd_i":true,"ps_sb_i":false,"ps_ifl":0.0,"ps_sa_i":false,"ps_shd":false,"ps_pbb":false,"ps_br_i":true,"ps_klt":true,"ps_bt_i":true,"ps_hd":0,"ps_kwn_i":false,"ps_ifl_i":true,"ps_ts":{"cv":{"op":"set","opValue":[]}},"ps_bbtw":null,"ps_lslm":0,"ps_bb":null,"ps_ls":1.15,"ps_bl":null,"ps_kwn":true,"ps_sm_i":true,"ps_il_i":true,"ps_sa":4.0,"ps_sb":14.0,"ps_br":null,"ps_ir_i":true,"ps_sd":null,"ps_bt":null,"ps_hdid":"","ps_lslm_i":true,"ps_sm":0,"ps_bbtw_i":true,"ps_al":0,"ps_bb_i":true,"ps_pbb_i":false,"ps_awao":false,"ps_il":0.0},"sdef_ts":{"ts_un":false,"ts_un_i":true,"ts_sc":false,"ts_it_i":true,"ts_fgc2":{"hclr_color":"#666666","clr_type":0},"ts_lsp_i":false,"ts_st_i":true,"ts_bgc2":{"hclr_color":null,"clr_type":0},"ts_fs_i":false,"ts_ff_i":true,"ts_it":false,"ts_va":"nor","ts_bgc2_i":true,"ts_bd_i":true,"ts_va_i":true,"ts_fs":12.0,"ts_ff":"Arial","ts_st":false,"ts_bd":false,"ts_sc_i":true,"ts_fgc2_i":false,"ts_lsp":{"lspp_points":0.0,"lsp_type":0},"ts_tw":400}}}, {"fontMetadataMap":{"Roboto Mono":{"fontFaces":[{"fontFamily":"Roboto Mono","weightedFontFamily":"Roboto Mono Thin","subset":"ALL","subsetValue":"*","sources":[{"url":"//fonts.gstatic.com/s/robotomono/v31/L0xuDF4xlVMF-BfR8bXMIhJHg45mwgGEFl0_3vuPRg.woff2","format":"woff2","isLocal":false}],"style":"normal","weight":100,"menuFont":false,"fontDrawSize":{"unitsPerEm":2048,"ascender":2146,"descender":-555,"lineGap":0,"STypoAscender":2146,"STypoDescender":-555,"STypoLineGap":0,"usWinAscent":2146,"usWinDescent":555,"fsSelection":64,"ulCodePageRange1":536871327,"cmapFormat2":false,"codepoint4e00":false}},{"fontFamily":"Roboto Mono","weightedFontFamily":"Roboto Mono ExtraLight","subset":"ALL","subsetValue":"*","sources":[{"url":"//fonts.gstatic.com/s/robotomono/v31/L0xuDF4xlVMF-BfR8bXMIhJHg45mwgGEFl0_XvqPRg.woff2","format":"woff2","isLocal":false}],"style":"normal","weight":200,"menuFont":false,"fontDrawSize":{"unitsPerEm":2048,"ascender":2146,"descender":-555,"lineGap":0,"STypoAscender":2146,"STypoDescender":-555,"STypoLineGap":0,"usWinAscent":2146,"usWinDescent":555,"fsSelection":64,"ulCodePageRange1":536871327,"cmapFormat2":false,"codepoint4e00":false}},{"fontFamily":"Roboto Mono","weightedFontFamily":"Roboto Mono Light","subset":"ALL","subsetValue":"*","sources":[{"url":"//fonts.gstatic.com/s/robotomono/v31/L0xuDF4xlVMF-BfR8bXMIhJHg45mwgGEFl0_gPqPRg.woff2","format":"woff2","isLocal":false}],"style":"normal","weight":300,"menuFont":false,"fontDrawSize":{"unitsPerEm":2048,"ascender":2146,"descender":-555,"lineGap":0,"STypoAscender":2146,"STypoDescender":-555,"STypoLineGap":0,"usWinAscent":2146,"usWinDescent":555,"fsSelection":64,"ulCodePageRange1":536871327,"cmapFormat2":false,"codepoint4e00":false}},{"fontFamily":"Roboto Mono","weightedFontFamily":"Roboto Mono Regular","subset":"ALL","subsetValue":"*","sources":[{"url":"//fonts.gstatic.com/s/robotomono/v31/L0xuDF4xlVMF-BfR8bXMIhJHg45mwgGEFl0_3vqPRg.woff2","format":"woff2","isLocal":false}],"style":"normal","weight":400,"menuFont":false,"fontDrawSize":{"unitsPerEm":2048,"ascender":2146,"descender":-555,"lineGap":0,"STypoAscender":2146,"STypoDescender":-555,"STypoLineGap":0,"usWinAscent":2146,"usWinDescent":555,"fsSelection":64,"ulCodePageRange1":536871327,"cmapFormat2":false,"codepoint4e00":false}},{"fontFamily":"Roboto Mono","weightedFontFamily":"Roboto Mono Medium","subset":"ALL","subsetValue":"*","sources":[{"url":"//fonts.gstatic.com/s/robotomono/v31/L0xuDF4xlVMF-BfR8bXMIhJHg45mwgGEFl0_7PqPRg.woff2","format":"woff2","isLocal":false}],"style":"normal","weight":500,"menuFont":false,"fontDrawSize":{"unitsPerEm":2048,"ascender":2146,"descender":-555,"lineGap":0,"STypoAscender":2146,"STypoDescender":-555,"STypoLineGap":0,"usWinAscent":2146,"usWinDescent":555,"fsSelection":64,"ulCodePageRange1":536871327,"cmapFormat2":false,"codepoint4e00":false}},{"fontFamily":"Roboto Mono","weightedFontFamily":"Roboto Mono SemiBold","subset":"ALL","subsetValue":"*","sources":[{"url":"//fonts.gstatic.com/s/robotomono/v31/L0xuDF4xlVMF-BfR8bXMIhJHg45mwgGEFl0_AP2PRg.woff2","format":"woff2","isLocal":false}],"style":"normal","weight":600,"menuFont":false,"fontDrawSize":{"unitsPerEm":2048,"ascender":2146,"descender":-555,"lineGap":0,"STypoAscender":2146,"STypoDescender":-555,"STypoLineGap":0,"usWinAscent":2146,"usWinDescent":555,"fsSelection":64,"ulCodePageRange1":536871327,"cmapFormat2":false,"codepoint4e00":false}},{"fontFamily":"Roboto Mono","weightedFontFamily":"Roboto Mono Bold","subset":"ALL","subsetValue":"*","sources":[{"url":"//fonts.gstatic.com/s/robotomono/v31/L0xuDF4xlVMF-BfR8bXMIhJHg45mwgGEFl0_Of2PRg.woff2","format":"woff2","isLocal":false}],"style":"normal","weight":700,"menuFont":false,"fontDrawSize":{"unitsPerEm":2048,"ascender":2146,"descender":-555,"lineGap":0,"STypoAscender":2146,"STypoDescender":-555,"STypoLineGap":0,"usWinAscent":2146,"usWinDescent":555,"fsSelection":32,"ulCodePageRange1":536871327,"cmapFormat2":false,"codepoint4e00":false}},{"fontFamily":"Roboto Mono","weightedFontFamily":"Roboto Mono Thin","subset":"ALL","subsetValue":"*","sources":[{"url":"//fonts.gstatic.com/s/robotomono/v31/L0xoDF4xlVMF-BfR8bXMIjhOsXG-q2oeuFoqFrlnAeW4.woff2","format":"woff2","isLocal":false}],"style":"italic","weight":100,"menuFont":false,"fontDrawSize":{"unitsPerEm":2048,"ascender":2146,"descender":-555,"lineGap":0,"STypoAscender":2146,"STypoDescender":-555,"STypoLineGap":0,"usWinAscent":2146,"usWinDescent":555,"fsSelection":1,"ulCodePageRange1":536871327,"cmapFormat2":false,"codepoint4e00":false}},{"fontFamily":"Roboto Mono","weightedFontFamily":"Roboto Mono ExtraLight","subset":"ALL","subsetValue":"*","sources":[{"url":"//fonts.gstatic.com/s/robotomono/v31/L0xoDF4xlVMF-BfR8bXMIjhOsXG-q2oeuFoqFrnnAOW4.woff2","format":"woff2","isLocal":false}],"style":"italic","weight":200,"menuFont":false,"fontDrawSize":{"unitsPerEm":2048,"ascender":2146,"descender":-555,"lineGap":0,"STypoAscender":2146,"STypoDescender":-555,"STypoLineGap":0,"usWinAscent":2146,"usWinDescent":555,"fsSelection":1,"ulCodePageRange1":536871327,"cmapFormat2":false,"codepoint4e00":false}},{"fontFamily":"Roboto Mono","weightedFontFamily":"Roboto Mono Light","subset":"ALL","subsetValue":"*","sources":[{"url":"//fonts.gstatic.com/s/robotomono/v31/L0xoDF4xlVMF-BfR8bXMIjhOsXG-q2oeuFoqFrk5AOW4.woff2","format":"woff2","isLocal":false}],"style":"italic","weight":300,"menuFont":false,"fontDrawSize":{"unitsPerEm":2048,"ascender":2146,"descender":-555,"lineGap":0,"STypoAscender":2146,"STypoDescender":-555,"STypoLineGap":0,"usWinAscent":2146,"usWinDescent":555,"fsSelection":1,"ulCodePageRange1":536871327,"cmapFormat2":false,"codepoint4e00":false}},{"fontFamily":"Roboto Mono","weightedFontFamily":"Roboto Mono Regular","subset":"ALL","subsetValue":"*","sources":[{"url":"//fonts.gstatic.com/s/robotomono/v31/L0xoDF4xlVMF-BfR8bXMIjhOsXG-q2oeuFoqFrlnAOW4.woff2","format":"woff2","isLocal":false}],"style":"italic","weight":400,"menuFont":false,"fontDrawSize":{"unitsPerEm":2048,"ascender":2146,"descender":-555,"lineGap":0,"STypoAscender":2146,"STypoDescender":-555,"STypoLineGap":0,"usWinAscent":2146,"usWinDescent":555,"fsSelection":1,"ulCodePageRange1":536871327,"cmapFormat2":false,"codepoint4e00":false}},{"fontFamily":"Roboto Mono","weightedFontFamily":"Roboto Mono Medium","subset":"ALL","subsetValue":"*","sources":[{"url":"//fonts.gstatic.com/s/robotomono/v31/L0xoDF4xlVMF-BfR8bXMIjhOsXG-q2oeuFoqFrlVAOW4.woff2","format":"woff2","isLocal":false}],"style":"italic","weight":500,"menuFont":false,"fontDrawSize":{"unitsPerEm":2048,"ascender":2146,"descender":-555,"lineGap":0,"STypoAscender":2146,"STypoDescender":-555,"STypoLineGap":0,"usWinAscent":2146,"usWinDescent":555,"fsSelection":1,"ulCodePageRange1":536871327,"cmapFormat2":false,"codepoint4e00":false}},{"fontFamily":"Roboto Mono","weightedFontFamily":"Roboto Mono SemiBold","subset":"ALL","subsetValue":"*","sources":[{"url":"//fonts.gstatic.com/s/robotomono/v31/L0xoDF4xlVMF-BfR8bXMIjhOsXG-q2oeuFoqFrm5B-W4.woff2","format":"woff2","isLocal":false}],"style":"italic","weight":600,"menuFont":false,"fontDrawSize":{"unitsPerEm":2048,"ascender":2146,"descender":-555,"lineGap":0,"STypoAscender":2146,"STypoDescender":-555,"STypoLineGap":0,"usWinAscent":2146,"usWinDescent":555,"fsSelection":1,"ulCodePageRange1":536871327,"cmapFormat2":false,"codepoint4e00":false}},{"fontFamily":"Roboto Mono","weightedFontFamily":"Roboto Mono Bold","subset":"ALL","subsetValue":"*","sources":[{"url":"//fonts.gstatic.com/s/robotomono/v31/L0xoDF4xlVMF-BfR8bXMIjhOsXG-q2oeuFoqFrmAB-W4.woff2","format":"woff2","isLocal":false}],"style":"italic","weight":700,"menuFont":false,"fontDrawSize":{"unitsPerEm":2048,"ascender":2146,"descender":-555,"lineGap":0,"STypoAscender":2146,"STypoDescender":-555,"STypoLineGap":0,"usWinAscent":2146,"usWinDescent":555,"fsSelection":33,"ulCodePageRange1":536871327,"cmapFormat2":false,"codepoint4e00":false}},{"fontFamily":"Roboto Mono--Menu","weightedFontFamily":"Roboto Mono","subset":"MENU","subsetValue":"menu","sources":[{"url":"//fonts.gstatic.com/s/robotomono/v31/L0xuDF4xlVMF-BfR8bXMIhJHg45mwgGEFl0_3vq_QuW4.woff2","format":"woff2","isLocal":false}],"style":"normal","weight":400,"menuFont":true,"fontDrawSize":{"unitsPerEm":2048,"ascender":2146,"descender":-555,"lineGap":0,"STypoAscender":2146,"STypoDescender":-555,"STypoLineGap":0,"usWinAscent":2146,"usWinDescent":555,"fsSelection":64,"ulCodePageRange1":536871327,"cmapFormat2":false,"codepoint4e00":false}}],"version":"v31","documentFont":true,"userDomainFont":false,"restrictedDomainFont":false,"hasInkedWhitespace":false,"genericFallback":"monospace"},"Comfortaa":{"fontFaces":[{"fontFamily":"Comfortaa","weightedFontFamily":"Comfortaa Light","subset":"ALL","subsetValue":"*","sources":[{"url":"//fonts.gstatic.com/s/comfortaa/v47/1Pt_g8LJRfWJmhDAuUsSQamb1W0lwk4S4TbMPrE.woff2","format":"woff2","isLocal":false}],"style":"normal","weight":300,"menuFont":false,"fontDrawSize":{"unitsPerEm":1000,"ascender":881,"descender":-234,"lineGap":0,"STypoAscender":881,"STypoDescender":-234,"STypoLineGap":0,"usWinAscent":1285,"usWinDescent":332,"fsSelection":192,"ulCodePageRange1":536871327,"cmapFormat2":false,"codepoint4e00":false}},{"fontFamily":"Comfortaa","weightedFontFamily":"Comfortaa Regular","subset":"ALL","subsetValue":"*","sources":[{"url":"//fonts.gstatic.com/s/comfortaa/v47/1Pt_g8LJRfWJmhDAuUsSQamb1W0lwk4S4WjMPrE.woff2","format":"woff2","isLocal":false}],"style":"normal","weight":400,"menuFont":false,"fontDrawSize":{"unitsPerEm":1000,"ascender":881,"descender":-234,"lineGap":0,"STypoAscender":881,"STypoDescender":-234,"STypoLineGap":0,"usWinAscent":1285,"usWinDescent":332,"fsSelection":192,"ulCodePageRange1":536871327,"cmapFormat2":false,"codepoint4e00":false}},{"fontFamily":"Comfortaa","weightedFontFamily":"Comfortaa Medium","subset":"ALL","subsetValue":"*","sources":[{"url":"//fonts.gstatic.com/s/comfortaa/v47/1Pt_g8LJRfWJmhDAuUsSQamb1W0lwk4S4VrMPrE.woff2","format":"woff2","isLocal":false}],"style":"normal","weight":500,"menuFont":false,"fontDrawSize":{"unitsPerEm":1000,"ascender":881,"descender":-234,"lineGap":0,"STypoAscender":881,"STypoDescender":-234,"STypoLineGap":0,"usWinAscent":1285,"usWinDescent":332,"fsSelection":192,"ulCodePageRange1":536871327,"cmapFormat2":false,"codepoint4e00":false}},{"fontFamily":"Comfortaa","weightedFontFamily":"Comfortaa SemiBold","subset":"ALL","subsetValue":"*","sources":[{"url":"//fonts.gstatic.com/s/comfortaa/v47/1Pt_g8LJRfWJmhDAuUsSQamb1W0lwk4S4bbLPrE.woff2","format":"woff2","isLocal":false}],"style":"normal","weight":600,"menuFont":false,"fontDrawSize":{"unitsPerEm":1000,"ascender":881,"descender":-234,"lineGap":0,"STypoAscender":881,"STypoDescender":-234,"STypoLineGap":0,"usWinAscent":1285,"usWinDescent":332,"fsSelection":192,"ulCodePageRange1":536871327,"cmapFormat2":false,"codepoint4e00":false}},{"fontFamily":"Comfortaa","weightedFontFamily":"Comfortaa Bold","subset":"ALL","subsetValue":"*","sources":[{"url":"//fonts.gstatic.com/s/comfortaa/v47/1Pt_g8LJRfWJmhDAuUsSQamb1W0lwk4S4Y_LPrE.woff2","format":"woff2","isLocal":false}],"style":"normal","weight":700,"menuFont":false,"fontDrawSize":{"unitsPerEm":1000,"ascender":881,"descender":-234,"lineGap":0,"STypoAscender":881,"STypoDescender":-234,"STypoLineGap":0,"usWinAscent":1285,"usWinDescent":332,"fsSelection":160,"ulCodePageRange1":536871327,"cmapFormat2":false,"codepoint4e00":false}},{"fontFamily":"Comfortaa--Menu","weightedFontFamily":"Comfortaa","subset":"MENU","subsetValue":"menu","sources":[{"url":"//fonts.gstatic.com/s/comfortaa/v47/1Pt_g8LJRfWJmhDAuUsSQamb1W0lwk4S4WjMDrUfIA.woff2","format":"woff2","isLocal":false}],"style":"normal","weight":400,"menuFont":true,"fontDrawSize":{"unitsPerEm":1000,"ascender":881,"descender":-234,"lineGap":0,"STypoAscender":881,"STypoDescender":-234,"STypoLineGap":0,"usWinAscent":1285,"usWinDescent":332,"fsSelection":192,"ulCodePageRange1":536871327,"cmapFormat2":false,"codepoint4e00":false}}],"version":"v47","documentFont":true,"userDomainFont":false,"restrictedDomainFont":false,"hasInkedWhitespace":false,"genericFallback":"cursive"}},"weightedFontFamilyMap":{"Comfortaa SemiBold":"Comfortaa","Roboto Mono Medium":"Roboto Mono","Comfortaa Bold":"Comfortaa","Comfortaa Regular":"Comfortaa","Roboto Mono ExtraLight":"Roboto Mono","Comfortaa Medium":"Comfortaa","Comfortaa Light":"Comfortaa","Roboto Mono SemiBold":"Roboto Mono","Roboto Mono Light":"Roboto Mono","Roboto Mono Thin":"Roboto Mono","Roboto Mono Bold":"Roboto Mono","Roboto Mono Regular":"Roboto Mono"},"unrecognizedFontFamilies":[]}, [null,null,null,null,null,null,null,0,["Thành Phát Nguyễn",null,"//lh3.googleusercontent.com/a/ACg8ocLr5NeV0Gj_S-5PWw3nwB9e9I6a0cxDwxyu1Vgl3p4otKQVqHQ\u003ds50-c-k-no","109015353153985414909",1,null,0,"thanhphatggclass@gmail.com",0,1],1,"AAHRpnXsA7_ylLZSCG_jaGyKCTlOUOzg4sCnYSJ4UY0m5Q9un6y4QdkmuzwF7FhsxitXnSTf2uVWNhJa89DpkSvxuBk-fu1tIyA06eCrVuBpZ_FJnq_wM8yo",null,null,null,null,null,0,null,null,1,null,null,null,null,null,1,null,null,null,1,null,null,1,null,null,null,null,1,1,null,null,null,null,["tf",60000,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,0,1,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,1,1,null,null,null,1,null,null,null,null,null,null,1,null,null,null,null,null,null,null,null,null,1],[[5700032,5700057,5700103,5700333,5702723,5703839,5703889,5704621,5704883,5705891,5706210,5706270,5706786,5707047,5707075,5707204,5708365,5708568,5709476,5713195,5713554,5714310,5715791,5717567,5723473,5724896,5725855,5727701,5729092,5734632,5737800,5742652,5743787,5746724,5753681,5754938,5758674,5760167,5760470,5763830,5781990,5799579,5799662,5799704,5799767,5799883,5799914,13702623,48966242,49398669,49421372,49472111,49491745,49498981,49518630,49623449,49629222,49644063,49661348,49769425,49822849,49833570,49842883,49894868,49904447,49924634,49926161,49943107,49972001,49974162,49979546,50223802,50266110,50273476,50293596,50297144,50389218,50439248,50538792,50550119,50561471,50562752,50578551,50587130,50602189,70973210,71061466,71079966,71085289,71120936,71145365,71200401,71289809,71291313,71316515,71330202,71331366,71376164,71387398,71387817,71406865,71451150,71505708,71520498,71528645,71530231,71544802,71558492,71608308,71628335,71628345,71628375,71628395,71628455,71628465,71638491,71652641,71657948,71659941,71675235,71679628,71689908,71710068,71710709,71825491,71832990,71854958,71897955,71899334,71960428,94327579,94353356,94394920,94409697,94434325,94460815,94461479,94586067,94629745,94640327,94692346,94733485,94734514,94819000,94819398,94819464,94871318,94919080,95087174,95099841,95104425,95111893,95112741,95113224,95129666,95131181,95182367,95261548,95299614,95309235,95314730,99251831,99257775,99291459,99317306,99338868,99342846,99343456,99368740,99400270,99402239,99440165,99440941,99457594,99457835,99778391,101448020,101448280,101478064,101489656,101489911,101492839,101540525,101561680,101562354,101575592,101631239,101632070,101687065,101739377,101755361,101755552,101781911,101788151,101793871,101823271,101847134,101858219,101860635,101867811,101868133,101887642,101889011,101917213,101919506,101920174,101933599,101962010,101962022,101962589,102013395,102030510,102043679,102043680,102059468,102070604,102070898,102083194,102146715,102161233,102161595,102178370,102198450,102201965,102205361,102205636,102208260,102236054,102244477,102280756,102287352,102292144,102322651,102343368,102388307,102399909,102402787,102448115,102449896,102469788,102517170,102548669,102554632,102554874,102587761,102618676,102641332,102649648,102649933,102672677,102673094,102685402,102685921,102721117,102722003,102727394,102761414,102762343,102783397,102787523,102807738,102811911,102858954,102867803,102883806,102902684,102903588,102909807,102925907,102926491,102934198,102944142,102944351,102944430,102944560,102944698,102944816,102956653,102972737,102986875,102988342,102988446,102988752,102988921,103011607,103015173,103018781,103039162,103163012,103175938,103285934,103286101,103286432,103288274,103289147,103289226,103299206,103326455,103339784,103343818,104530118,104574047,104575402,104575548,104575644,104575925,104615563,104627942,104654334,104699532,104746807,104761739,104790721,104798723,104842314,104907779,104963470,104976565,104983097,105082065,105083776,105084997,105087636,105111916,105125345,105180821,105197504,105228950,105250953,105255706,105283755,105292984,105345089,105346133,105360306,105374012,105393839,105439299,105461790,115511316,115601177,115626407,115665137,115665210,115666025,115669426,115680658,115738046,115755272,115780029,115780486,115797570,115821555,115849193,115854836,115902269,115909789,115916255,115917366,115928907,115955876,116062778,116068766,116076825,116076931,116150713,116176897,116221362,116229722,116250793,116276289,116307752,116339549,116372465,116400394,116407665,116416022,116418255,116426540,116459613,116480001,116502867,116508647,116514189,116538564,116567540,116569208,116593749,116609399,116699417,116714309,116714312,116731455,116731469,116731483,116731497,116731511,116731525,116757444,116778962,116780655,116780669,116780683,116780697,116780711,116783791,116840725,116845812,116874397,116901210,116901291,116902255,116907338,116908904,116909018,116909339,116910313,116929361,116929943,116930004,116980535,116980953,116981295,116981596,116981877,116982440,116982641,116982922,116983123,116983324,116984756,116986178,116988232,117000940,117010861,117072309,117148952,117205515,117224107,117249338,117269065,117284164,117286739,117290318,117297874,117309242,117411966,117419531,117457926,117474414,117503105,117504351,117547529,117586574,117642597,117668280,117756763,117852668,117879418,117895928,117970102,117983437,117995685,118026977,118029479,118057581,118094467,118098337,118109056,118110662,118127429,118132049,118144980,118155115,118181395,118183989,118189664,118199438,118213591,118237163,118240044,118281132,118291744,118292268,118292282,118311127,118368045,118368704,118419988,118452697,118489188,118497535,118528668,118531377,118555995,118566900,118567242,118567312,118575744,118579784,118596023,118599489,118627821,118656885,118781281,118795508,118824725,118925913,118938369,118952717,118957777,118960876,118965154,119022220,119058022,119096529,119140592,119170349,119194834,119202566,119207503,119207509,119208307,119209377,119220909,119220911,119220913,119220915,119220917,119220919,119235569,119237709,119262907,119262908,119287101,119311199,119317356,119350208,119367401,119370737,119418569,119419030,119419133,119422199,119455939,119464292,119465397,119474822,119525133,119528290,119530188,119538776,119546479,119546486,119566076,119590807,119590826,119595195,119597439,119598478,119605178,119608200,119609090,119612423,119614703,119615546,119636415,119667320,119671618,119680688,119685921,119702657,119707161,119741503,119770493,119799521,119804032,119805096,119826325,119844376,119854012,119866144,119869968,119885477,119908997,119914643,119919349,119941529,119967977,119976759,119988641,120001478,120008601,120008659,120025503,120049019,120050618,120074344,120091101,120120566,120140755,120156657,120161435,120163564,120164830,120169628,120175695,120182626,120190047,120191506,120250430,120251575,120257690,120261437,120272513,120275439,120286671,120300745,120362078,120362087,120380565,120380591,120395965,120400587,120401957,120403609,120407879,120408014,120418108,120426046,120426060,120426088,120426102,120426116,120459269,120533756,120603287,120612091,120618046,120619851,120626042,120644050,120645696,120647308,120650551,120664795,120674439,120674441,120674443,120674444,120674447,120674449,120690294,120700415,120711531,120715208,120716771,120730652,120730872,120731459,120735857,120773182,120775013,120777470,120797125,120797284,120813570,120815334,120822134,120822778,120846904,120867286,120887330,120900500,120911956,120912896,120917339,120917748,120917780,120932919,120932945,120933033,120935177,120935298,120935312,120935326,120941658,120951792,120965199,120969800,120982230,120985574,120988609,121008560,121021731,121030127,121031517,121045318,121047677,121050659,121055791,121066107,121069350,121070469,121091273,121092128,121107449,121110310,121118935,121120944,121123093,121123412,121125649,121127289,121134704,121169160,121169713,121172899,121177637,121191760,121208634,121237105,121246235,121255411,121265318,121269511,121277057,121283671,121286315,121297154,121297170,121299957,121307186,121330948,121339453,121346197,121350603,121350890,121359802,121372719,121373720,121376237,121377267,121378116,121380106,121380118,121381399,121381430,121381439,121381950,121383013,121384675,121398715,121402372,121406132,121416078,121416971,121426016,121426981,121429273,121431631,121436763,121440940,121442162,121445031,121451520,121453928,121459973,121460129,123238631,123249503,123251668,123255690,123259962,123270156,123282066,123287469,123303697,123306718,123315569,123324706,123334254,123334404,123336947,123338844,123346279,123356945,123363048,123364527,123364579,123364729,123371158,123389389,123394435,123446356,123451226,123466806,123468156,123476831,123477308,123482268,123487307,123490786,123505035,123522867,123525081,123541682,123541757,123546721,123564492,123604578,123604856,123606890,123636677,123655952,123656659,123675620,123693816,123696709,123702476,123772081,123857718,123903824,123940671,123947909,124008410,124029914,124043339,124047227,124092094]],null,null,null,null,4], undefined , [{"keyPath":["syncMap","applicationFonts","1"],"state":{"hashValue":"00000000"},"data":{"value":[]}},{"keyPath":["syncMap","domainFonts","0"],"state":{"hashValue":"00000000"},"data":{"value":[]}},{"keyPath":["syncMap","preferences","docs-fonts"],"state":{"timestamp":1766069357550587},"data":{"familyList":["Alegreya","Amatic SC","Bree Serif","Calibri","Cambria","Merriweather","Permanent Marker","Pinyon Script","Playfair Display","Proxima Nova","Roboto","Roboto Mono","Ultra","Varela Round"],"recentlyUsedFamilyList":["Arial","Comfortaa"],"transitionedFamilyList":[],"hasTransitioned":true}},{"keyPath":["syncMap","preferences","docs-autocorrect"],"state":{"timestamp":1746428519497531},"data":{"autocorrectLinks":true,"autocorrectSmartQuotes":true,"enableStringReplacement":true,"ruleList":[],"autocorrectParagraphDirection":true,"autocorrectLists":true,"autocorrectAutoCapitalization":true,"autocorrectSpelling":true,"autocorrectSmartChip":true,"autocorrectMarkdown":false,"showMarkdownPromo":false}},{"keyPath":["syncMap","preferences","docs-mention-dsp"],"state":{"timestamp":1794339413601579},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-enable_colon_emoji_insertion"],"state":{"timestamp":1794339413601579},"data":{"value":true}},{"keyPath":["syncMap","preferences","docs-dlpc"],"state":{"timestamp":1794339413601582},"data":{"value":true}},{"keyPath":["syncMap","preferences","docs-essentials-promo-shown-count"],"state":{"timestamp":1794339413601584},"data":{"value":0}},{"keyPath":["syncMap","preferences","mae-show_addons_menu_promo"],"state":{"timestamp":1794339413601585},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-email_menu_new_badge_shown_count"],"state":{"timestamp":1794339413601587},"data":{"value":0}},{"keyPath":["syncMap","preferences","docs-shadow_docs_reply_with_file_promo_last_shown_millis"],"state":{"timestamp":1794339413601587},"data":{"value":0}},{"keyPath":["syncMap","preferences","docs-shadow_docs_reply_with_file_new_badge_dismissed"],"state":{"timestamp":1794339413601587},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-homescreen-aips"],"state":{"timestamp":1794339413601588},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-homescreen-asp","1"],"state":{"timestamp":1792381928061530},"data":{"shadowDocsOnboardingPromo":{"dismissed":true,"shownCount":0}}},{"keyPath":["syncMap","preferences","docs-homescreen-ftbdct"],"state":{"timestamp":1794339413601588},"data":{"value":0}},{"keyPath":["syncMap","preferences","docs-homescreen-dbs"],"state":{"timestamp":1794339413601588},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-homescreen-hpb"],"state":{"timestamp":1794339413601588},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-homescreen-ht"],"state":{"timestamp":1794339413601588},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-homescreen-wws"],"state":{"timestamp":1794339413601588},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-etg-lvt"],"state":{"timestamp":1794339413601598},"data":{"value":0}},{"keyPath":["syncMap","preferences","docs-unsupported_feature_warning_callout_bubble_shown_count"],"state":{"timestamp":1762511541391535},"data":{"value":3}},{"keyPath":["syncMap","preferences","docs-office_editing_file_extension_promo_badge_shown_count"],"state":{"timestamp":1762511541391535},"data":{"value":3}},{"keyPath":["syncMap","preferences","docs-esbas"],"state":{"timestamp":1794339413601609},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-chrome","2"],"state":{"timestamp":1760968808380410},"data":{"viewMode":2}},{"keyPath":["syncMap","preferences","docs-chrome","1"],"state":{"timestamp":1760968808380410},"data":{"viewMode":1}},{"keyPath":["syncMap","preferences","docs-voice_tips_shown"],"state":{"timestamp":1794339413601621},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-web_speech_api_notice_seen"],"state":{"timestamp":1794339413601621},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-web_speech_api_notice_v2_seen"],"state":{"timestamp":1794339413601621},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-user_dictionary"],"state":{"timestamp":1794339413601622},"data":{"wordList":[]}},{"keyPath":["syncMap","preferences","docs-show_proofread_summary_bubble"],"state":{"timestamp":1794339413601624},"data":{"value":true}},{"keyPath":["syncMap","preferences","docs-proofread_summary_last_seen_time_ms"],"state":{"timestamp":1794339413601624},"data":{"value":0}},{"keyPath":["syncMap","preferences","docs-enable_screen_magnifier"],"state":{"timestamp":1794339413601627},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-approvals_new_badge_click_count"],"state":{"timestamp":1794339413601628},"data":{"value":0}},{"keyPath":["syncMap","preferences","docs-eawsc"],"state":{"timestamp":1794339413601629},"data":{"value":true}},{"keyPath":["syncMap","preferences","docs-smart_compose_inline_promo_shown_count"],"state":{"timestamp":1794339413601629},"data":{"value":0}},{"keyPath":["syncMap","preferences","docs-smart_compose_mobile_inline_promo_shown_count"],"state":{"timestamp":1794339413601629},"data":{"value":0}},{"keyPath":["syncMap","preferences","docs-smart_compose_docos_inline_promo_shown_count"],"state":{"timestamp":1794339413601629},"data":{"value":0}},{"keyPath":["syncMap","preferences","docs-smart_compose_sketchy_inline_promo_shown_count"],"state":{"timestamp":1794339413601629},"data":{"value":0}},{"keyPath":["syncMap","preferences","docs-enable_scoped_screen_reader","1"],"state":{"timestamp":1740755022757280},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-enable_scoped_screen_reader","5"],"state":{"timestamp":1740755022757280},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-enable_scoped_screen_reader","2"],"state":{"timestamp":1740755022757280},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-enable_scoped_screen_reader","3"],"state":{"timestamp":1740755022757280},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-enable_scoped_braille","1"],"state":{"timestamp":1740755022757280},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-enable_scoped_braille","5"],"state":{"timestamp":1740755022757280},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-enable_scoped_braille","2"],"state":{"timestamp":1740755022757280},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-enable_scoped_braille","3"],"state":{"timestamp":1740755022757280},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-enable_scoped_unified_accessibility","1"],"state":{"timestamp":1740755022757280},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-enable_scoped_unified_accessibility","5"],"state":{"timestamp":1740755022757280},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-enable_scoped_unified_accessibility","2"],"state":{"timestamp":1740755022757280},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-enable_scoped_unified_accessibility","3"],"state":{"timestamp":1740755022757280},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-screenreader"],"state":{"timestamp":1740755022757280},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-enable_braille"],"state":{"timestamp":1740755022757280},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-mute_collaborators"],"state":{"timestamp":1740755022757280},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-companion","5"],"state":{"timestamp":1762238143954595},"data":{"openStatus":1,"lastOpenedGuestAppId":0}},{"keyPath":["syncMap","preferences","docs-companion","1"],"state":{"timestamp":1762238143954595},"data":{"openStatus":1,"lastOpenedGuestAppId":0}},{"keyPath":["syncMap","preferences","docs-third_party_link_privacy_promo_shown"],"state":{"timestamp":1794339413601649},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-show-encrypted-create-dialog"],"state":{"timestamp":1794339413601651},"data":{"value":true}},{"keyPath":["syncMap","preferences","docs-should_display_policy_viz_callout"],"state":{"timestamp":1759602949820601},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-global_promos","0"],"state":{"timestamp":1792381928061530},"data":{"meetInEditorsVideoPromo":{"dismissed":true,"shownCount":0},"punchLivePointerPromo":{"dismissed":true,"shownCount":0},"chaptersReaderPromo":{"dismissed":true,"shownCount":0},"chaptersCreatorPromo":{"dismissed":true,"shownCount":0},"Add Reaction Promo":{"dismissed":true,"shownCount":0},"contentLibraryRailPromo":{"dismissed":true,"shownCount":0}}},{"keyPath":["syncMap","preferences","kix-ap","1"],"state":{"timestamp":1792381928061530},"data":{"shadowDocsOnboardingPromo":{"dismissed":true,"shownCount":0}}},{"keyPath":["syncMap","preferences","docs-display-date-suggestion-hint"],"state":{"timestamp":1794339413601579},"data":{"value":true}},{"keyPath":["syncMap","preferences","docs-date-chip-pattern-type"],"state":{"timestamp":1794339413601579},"data":{"value":0}},{"keyPath":["syncMap","preferences","kix-nwv"],"state":{"timestamp":1793038984780264},"data":{"value":1}},{"keyPath":["syncMap","preferences","kix-nv"],"state":{"timestamp":1793038984780264},"data":{"value":true}},{"keyPath":["syncMap","preferences","kix-sdmw"],"state":{"timestamp":1793038984780264},"data":{"value":false}},{"keyPath":["syncMap","preferences","kix-dmwsm"],"state":{"timestamp":1793038984780264},"data":{"value":0}},{"keyPath":["syncMap","preferences","kix-sr"],"state":{"timestamp":1793038984780264},"data":{"value":true}},{"keyPath":["syncMap","preferences","kix-ssc2"],"state":{"timestamp":1793038984780264},"data":{"value":true}},{"keyPath":["syncMap","preferences","kix-ssu"],"state":{"timestamp":1793038984780264},"data":{"value":true}},{"keyPath":["syncMap","preferences","kix-sgc"],"state":{"timestamp":1793038984780264},"data":{"value":true}},{"keyPath":["syncMap","preferences","kix-sgu"],"state":{"timestamp":1793038984780264},"data":{"value":true}},{"keyPath":["syncMap","preferences","kix-wcs"],"state":{"timestamp":1793038984780264},"data":{"value":true}},{"keyPath":["syncMap","preferences","kix-sis"],"state":{"timestamp":1793038984780264},"data":{"value":true}},{"keyPath":["syncMap","preferences","kix-scs"],"state":{"timestamp":1793038984780264},"data":{"value":true}},{"keyPath":["syncMap","preferences","kix-ssss"],"state":{"timestamp":1793038984780264},"data":{"value":true}},{"keyPath":["syncMap","preferences","kix-spvs"],"state":{"timestamp":1793038984780264},"data":{"value":true}},{"keyPath":["syncMap","preferences","kix-ssc"],"state":{"timestamp":1793038984780264},"data":{"value":true}},{"keyPath":["syncMap","preferences","kix-slhfwd"],"state":{"timestamp":1793038984780264},"data":{"value":true}},{"keyPath":["syncMap","preferences","kix-vm"],"state":{"timestamp":1793038984780264},"data":{"value":1}},{"keyPath":["syncMap","preferences","kix-ciips"],"state":{"timestamp":1793038984780264},"data":{"value":false}},{"keyPath":["syncMap","preferences","kix-wps"],"state":{"timestamp":1793038984780264},"data":{"value":true}},{"keyPath":["syncMap","preferences","kix-utcc"],"state":{"timestamp":1793038984780264},"data":{"value":0}},{"keyPath":["syncMap","preferences","kix-edips"],"state":{"timestamp":1793038984780264},"data":{"value":false}},{"keyPath":["syncMap","preferences","kix-edpasps"],"state":{"timestamp":1793038984780264},"data":{"value":false}},{"keyPath":["syncMap","preferences","kix-tops"],"state":{"timestamp":1793038984780264},"data":{"value":false}},{"keyPath":["syncMap","preferences","kix-ucipsc"],"state":{"timestamp":1793038984780264},"data":{"value":0}},{"keyPath":["syncMap","preferences","kix-udpsc"],"state":{"timestamp":1793038984780264},"data":{"value":3}},{"keyPath":["syncMap","preferences","kix-uedpsc"],"state":{"timestamp":1793038984780264},"data":{"value":1}},{"keyPath":["syncMap","preferences","kix-sdcic"],"state":{"timestamp":1793038984780264},"data":{"value":true}},{"keyPath":["syncMap","preferences","kix-sdcric"],"state":{"timestamp":1793038984780264},"data":{"value":true}},{"keyPath":["syncMap","preferences","kix-ueupsc"],"state":{"timestamp":1793038984780264},"data":{"value":0}},{"keyPath":["syncMap","preferences","kix-uspsc"],"state":{"timestamp":1793038984780264},"data":{"value":0}},{"keyPath":["syncMap","preferences","kix-npcps"],"state":{"timestamp":1793038984780264},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-docos-view-mode"],"state":{"timestamp":1793038984780264},"data":{"value":0}},{"keyPath":["syncMap","preferences","kix-ulots"],"state":{"timestamp":1793038984780264},"data":{"value":0}},{"keyPath":["syncMap","preferences","kix-aoss"],"state":{"timestamp":1793038984780264},"data":{"value":false}},{"keyPath":["syncMap","preferences","kix-ssaoc"],"state":{"timestamp":1793038984780264},"data":{"value":0}},{"keyPath":["syncMap","preferences","kix-swcdm"],"state":{"timestamp":1793038984780264},"data":{"value":false}},{"keyPath":["syncMap","preferences","kix-uscbpsc"],"state":{"timestamp":1793038984780264},"data":{"value":0}},{"keyPath":["syncMap","preferences","kix-hccpb"],"state":{"timestamp":1793038984780264},"data":{"value":false}},{"keyPath":["syncMap","preferences","kix-pfvps"],"state":{"timestamp":1793038984780264},"data":{"value":false}},{"keyPath":["syncMap","preferences","kix-pfhps"],"state":{"timestamp":1793038984780264},"data":{"value":false}},{"keyPath":["syncMap","preferences","kix-asds"],"state":{"timestamp":1793038984780264},"data":{"value":false}},{"keyPath":["syncMap","preferences","kix-aspsc"],"state":{"timestamp":1793038984780264},"data":{"value":0}},{"keyPath":["syncMap","preferences","kix-ttspsc"],"state":{"timestamp":1793038984780264},"data":{"value":0}},{"keyPath":["syncMap","preferences","kix-apprp"],"state":{"timestamp":1793038984780264},"data":{"value":100}},{"keyPath":["syncMap","preferences","kix-gmpm"],"state":{"timestamp":1793038984780264},"data":{"value":1}},{"keyPath":["syncMap","preferences","kix-cps"],"state":{"timestamp":1793038984780264},"data":{"value":false}},{"keyPath":["syncMap","preferences","kix-mdlsts"],"state":{"timestamp":1793038984780264},"data":{"value":0}},{"keyPath":["syncMap","preferences","kix-cfodsc"],"state":{"timestamp":1793038984780264},"data":{"value":1}},{"keyPath":["syncMap","preferences","kix-ps"],"state":{"timestamp":1794339413601684},"data":{"documentStyle":{}}},{"keyPath":["syncMap","preferences","kix-df"],"state":{"timestamp":1794339413601686},"data":{"proto":"[]"}},{"keyPath":["syncMap","preferences","kix-ss"],"state":{"timestamp":1794339413601687},"data":{"defaultStyleSetId":"","styleSetList":[]}},{"keyPath":["syncMap","preferences","kix-scsn"],"state":{"timestamp":1794339413601688},"data":{"proto":"[]"}},{"keyPath":["syncMap","preferences","kix-scpmpc"],"state":{"timestamp":1793038984780264},"data":{"value":0}},{"keyPath":["syncMap","preferences","kix-mnrmpc"],"state":{"timestamp":1793038984780264},"data":{"value":0}},{"keyPath":["syncMap","preferences","kix-npc"],"state":{"timestamp":1794339413601690},"data":{"proto":"[]"}},{"keyPath":["syncMap","preferences","kix-ctpsc"],"state":{"timestamp":1793038984780264},"data":{"value":0}},{"keyPath":["syncMap","preferences","kix-mdftcsc"],"state":{"timestamp":1793038984780264},"data":{"value":0}},{"keyPath":["syncMap","preferences","kix-mdftccsc"],"state":{"timestamp":1793038984780264},"data":{"value":0}},{"keyPath":["syncMap","preferences","docs-gemini-nudge"],"state":{"timestamp":1794339413601691},"data":{}},{"keyPath":["syncMap","preferences","sketchy-ruler"],"state":{"timestamp":1794339413601692},"data":{"value":true}},{"keyPath":["syncMap","preferences","sketchy-use_custom_auto_fit_types"],"state":{"timestamp":1794339413601692},"data":{"value":false}},{"keyPath":["syncMap","preferences","sketchy-textbox_auto_fit_type"],"state":{"timestamp":1794339413601692},"data":{"value":2}},{"keyPath":["syncMap","preferences","sketchy-textbox_margin"],"state":{"timestamp":1794339413601693},"data":{"proto":"[]"}},{"keyPath":["syncMap","preferences","docs-display_density"],"state":{"timestamp":1794339413601697},"data":{"value":0}},{"keyPath":["syncMap","preferences","docs-display_density_new_badge_click_count"],"state":{"timestamp":1794339413601697},"data":{"value":0}},{"keyPath":["syncMap","preferences","docs-ultrabox-shown-count"],"state":{"timestamp":1794339413601698},"data":{"value":0}},{"keyPath":["syncMap","preferences","docs-eawsr"],"state":{"timestamp":1794339413601699},"data":{"value":true}},{"keyPath":["syncMap","preferences","kix-ssps"],"state":{"timestamp":1794339413601701},"data":{"value":false}},{"keyPath":["syncMap","preferences","docs-emoji-sparse-map"],"state":{"timestamp":1794339413602444},"data":{"emojiSparseMapList":[]}},{"keyPath":["syncMap","preferences","docs-emoji_reation_promo_shown_count"],"state":{"timestamp":1746427061619051},"data":{"value":3}},{"keyPath":["syncMap","preferences","kix-ttsvn"],"state":{"timestamp":1793038984780264},"data":{"value":0}},{"keyPath":["syncMap","preferences","docs-should_display_disablement_callout"],"state":{"timestamp":1794339413601717},"data":{"value":true}}],  true , undefined , "t.0" , 5 , undefined , undefined , undefined , undefined , false , 0 , 0 , 0 ); DOCS_warmStartDocumentLoader = KX_kixApp.getWarmStartDocumentLoader(); DOCS_cseBlobArtifactsHolder = KX_kixApp.getCseBlobArtifactsHolder(); DOCS_initialLoadTiming = KX_kixApp.getInitialLoadTiming(); KX_getLatencyStats = function() {return KX_kixApp.getPerformanceManager().getInitialLoadStats();}; KX_resize = function() {KX_kixApp.resize();}; _docs_annotate_getAnnotatedText = function(id) {return KX_kixApp.getAnnotatedText(id);};}DOCS_timing['sac'] = new Date().getTime(); KX_startKix(); DOCS_initialLoadTiming.setTime('eac');</script>
                # you can use the 'downloadable' : true to determine
                match = re.search(
                    r"""['"]downloadable['"]\s*:\s*(true|false)""",
                    await page.content(),
                    re.IGNORECASE,
                )
                if match:
                    return match.group(1).lower() == "true"
                return False
            elif self.document_type == DocumentType.XLSX:
                # keep this quote unchanged
                # not found yet
                # implement later
                return False  # temp
        elif self.website_type == WebsiteType.SCRIBD:
            return False  # always false because scribd does not allow
        return False

    def get_fileID(self, url: str | None = None) -> str | None:
        """Extract a Google Drive, Google Docs, or Scribd file ID from a URL."""
        target_url = url or (self.page.url if self.page else None)
        if not target_url:
            return None

        parsed = urlparse(target_url)
        path_parts = [part for part in parsed.path.split("/") if part]

        # 1. Google Drive & Google Docs: .../d/{fileID}/...
        # e.g. drive.google.com/file/d/{ID}/view
        #      docs.google.com/document/d/{ID}/view
        #      docs.google.com/spreadsheets/d/{ID}/view
        if "d" in path_parts:
            id_index = path_parts.index("d") + 1
            if id_index < len(path_parts):
                return path_parts[id_index]

        # 2. Google Drive folder: .../folders/{folderID}...
        if "folders" in path_parts:
            id_index = path_parts.index("folders") + 1
            if id_index < len(path_parts):
                return path_parts[id_index]

        # 3. Scribd: .../document/{fileID}/... or .../doc/{fileID}/...
        for marker in ("document", "doc"):
            if marker in path_parts:
                id_index = path_parts.index(marker) + 1
                if id_index < len(path_parts) and path_parts[id_index] != "d":
                    return path_parts[id_index]

        # 4. Fallback for query parameters (e.g. drive.google.com/open?id={ID})
        query_params = parse_qs(parsed.query)
        if "id" in query_params and query_params["id"]:
            return query_params["id"][0]

        return None

    async def get_website_type(self, url: str | None = None) -> WebsiteType:
        """Determine the website type based on URL domain or HTML metadata in <head>.

        If *url* is provided the domain is checked first (fast path).
        Otherwise the method falls back to ``self.page.url`` and, as a last
        resort, inspects the raw HTML for characteristic metadata.
        """
        # --- fast path: explicit URL ------------------------------------------
        if url is not None:
            website = self.__match_domain(url)
            if website is not None:
                return website

        # --- fallback: page URL -----------------------------------------------
        page_url = self.page.url
        if page_url:
            website = self.__match_domain(page_url)
            if website is not None:
                return website

        # --- last resort: inspect HTML metadata --------------------------------
        html = await self.page.content()

        # Google Drive: <meta property="og:url" content="https://drive.google.com/file/d/....../">
        if "drive.google.com" in html:
            return WebsiteType.GOOGLE_DRIVE

        # Google Docs / Sheets:
        if "docs.google.com" in html:
            return WebsiteType.GOOGLE_DOC

        # Scribd: <link rel="search" href="https://www.scribd.com/opensearch.xml" ...>
        if "scribd.com" in html:
            return WebsiteType.SCRIBD

        raise ValueError(
            "Unable to determine website type: page is neither Google Drive nor Scribd"
        )

    @staticmethod
    def __match_domain(url: str) -> WebsiteType | None:
        """Return the WebsiteType matching *url*'s domain, or None."""
        domain = urlparse(url).netloc.lower()
        if "drive.google.com" in domain:
            return WebsiteType.GOOGLE_DRIVE
        if "docs.google.com" in domain:
            return WebsiteType.GOOGLE_DOC
        if "scribd.com" in domain:
            return WebsiteType.SCRIBD
        return None

    # ------------------------------------------------------------------
    # Page title
    # ------------------------------------------------------------------

    async def get_page_title(self) -> str:
        """Extract the <title> text from the page's <head>.

        Uses Playwright's built-in ``page.title()`` for performance.
        """
        title = await self.page.title()
        if not title:
            raise ValueError("Page does not contain a non-empty <title>")
        return title

    # ------------------------------------------------------------------
    # File name
    # ------------------------------------------------------------------

    async def get_file_name(self, html_template: str | None = None) -> str:
        """Extract the document file name from the page title.

        If *html_template* is provided the title is parsed from the raw HTML
        string (using ``_TitleParser``).  Otherwise the live Playwright page
        title is used.

        Google Drive – strips the `` - Google Drive`` suffix and returns the
        bare file name (preserving the original extension).

        Scribd – takes the first pipe-delimited segment of the title (the
        document name) and appends ``.pdf``.
        """
        if html_template is None and self.page is None:
            return self.fileID or "document"

        if html_template is not None:
            parser = _TitleParser()
            parser.feed(html_template)
            title = "".join(parser.title_parts).strip()
            if not title:
                raise ValueError("HTML template does not contain a non-empty <title>")
        else:
            title = await self.get_page_title()

        if self.website_type == WebsiteType.GOOGLE_DRIVE:
            # e.g. "document.pdf - Google Drive" → "document.pdf"
            return title.removesuffix(" - Google Drive").strip()

        if self.website_type == WebsiteType.GOOGLE_DOC:
            # e.g. "Doc Name - Google Docs" or "Doc Name - Google Tài liệu"
            name = re.sub(r"\s*-\s*Google\s+.*$", "", title).strip()
            return name or title.strip()

        if self.website_type == WebsiteType.SCRIBD:
            # e.g. "Doc Name | PDF | Tag1 | Tag2" → "Doc Name.pdf"
            name = title.split("|")[0].strip()
            return f"{name}.pdf"

        raise ValueError(f"Unsupported website type: {self.website_type}")

    # ------------------------------------------------------------------
    # Total pages
    # ------------------------------------------------------------------

    async def get_total_pages(self) -> int:
        """Extract the total page count from the document viewer.

        Google Drive – reads the ``<span jsname="Dt5gRb">`` element that
        displays the total page count (works for .doc, .pptx, .pdf viewers).

        Scribd – reads the ``<div data-e2e="total-pages">`` element.
        """
        if self.website_type == WebsiteType.GOOGLE_DRIVE:
            return await self.__get_total_pages_gd()

        if self.website_type == WebsiteType.GOOGLE_DOC:
            return await self.__get_total_pages_gdoc()

        if self.website_type == WebsiteType.SCRIBD:
            return await self.__get_total_pages_scribd()

        raise ValueError(f"Unsupported website type: {self.website_type}")

    async def __get_total_pages_gd(self) -> int:
        """Extract total pages from the Google Drive document viewer.

        The viewer toolbar contains::

            <span>/</span><span jsname="Dt5gRb">8</span>

        Uses a Playwright locator with a wait so the element is guaranteed to
        be present in the DOM before reading.
        """
        if self.document_type == DocumentType.PDF:
            locator = self.page.locator('span[jsname="Dt5gRb"]')
            return await self.__read_positive_page_count(
                locator, 'span[jsname="Dt5gRb"]', "Google Drive viewer"
            )
        elif self.document_type in (
            DocumentType.MICROSOFT_WORD,
            DocumentType.GOOGLE_DOC,
        ):
            try:
                locator = self.page.locator('span[jsname="Dt5gRb"]')
                await locator.first.wait_for(state="attached", timeout=30_000)
                if await locator.count() > 0:
                    return await self.__read_positive_page_count(
                        locator, 'span[jsname="Dt5gRb"]', "Google Drive viewer"
                    )
            except Exception:
                pass
            return 1
        return 1

    async def __get_total_pages_gdoc(self) -> int:
        """Extract total pages from the Google Docs viewer/editor."""
        try:
            locator = self.page.locator('span[jsname="Dt5gRb"]')
            if await locator.count() > 0:
                return await self.__read_positive_page_count(
                    locator, 'span[jsname="Dt5gRb"]', "Google Docs viewer"
                )
        except Exception:
            pass

        try:
            count = await self.page.locator(".kix-page").count()
            if count > 0:
                return count
        except Exception:
            pass

        return 100

    async def __get_total_pages_scribd(self) -> int:
        """Extract total pages from the Scribd document viewer.

        The viewer contains::

            <div data-e2e="total-pages" class="_3dwpfv">5</div>

        Uses a Playwright locator with a wait so the element is guaranteed to
        be present in the DOM before reading.
        """
        locator = self.page.locator('div[data-e2e="total-pages"]')
        return await self.__read_positive_page_count(
            locator, 'div[data-e2e="total-pages"]', "Scribd viewer"
        )

    async def __read_positive_page_count(
        self, locator, selector: str, viewer_name: str
    ) -> int:
        """Wait for a viewer's page-count element to contain a valid number."""
        await locator.wait_for(state="attached", timeout=30_000)
        await self.page.wait_for_function(
            """selector => [...document.querySelectorAll(selector)].some(
                element => /^\\s*\\d+\\s*$/.test(element.textContent || '') &&
                    Number(element.textContent) > 0
            )""",
            arg=selector,
            timeout=30_000,
        )

        for text in await locator.all_text_contents():
            match = re.fullmatch(r"\s*(\d+)\s*", text)
            if match and int(match.group(1)) > 0:
                return int(match.group(1))

        raise ValueError(
            f"Could not extract a positive total page count from {viewer_name}"
        )
