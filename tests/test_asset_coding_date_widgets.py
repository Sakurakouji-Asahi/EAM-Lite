from datetime import date

import pytest
from django.utils import timezone, translation

from apps.assets.custody_views import CustodyReturnForm
from apps.assets.forms import AssetDraftForm
from apps.finance.forms import FinanceDraftForm
from apps.masterdata.forms import AssetCodingSchemeForm
from tests.test_unified_asset_identity import context, registered

pytestmark = pytest.mark.django_db(transaction=True)


def test_date_controls_keep_iso_values_under_chinese_locale(context):
    with translation.override("zh-hans"):
        form = AssetDraftForm(actor=context["equipment"], company=context["company"],
            initial={"acquisition_date":date(2020,8,1),"commissioning_date":date(2020,9,1)})
        assert 'value="2020-08-01"' in str(form["acquisition_date"])
        assert 'value="2020-09-01"' in str(form["commissioning_date"])
        scheme = AssetCodingSchemeForm(actor=context["admin"], company=context["company"], instance=context["standard_scheme"])
        assert f'value="{timezone.localdate().isoformat()}"' in str(scheme["effective_from"])
        asset = registered(context)
        finance = FinanceDraftForm(actor=context["finance"], company=context["company"],asset=asset,
            initial={"commissioning_date":date(2020,9,1)})
        assert 'value="2020-09-01"' in str(finance["commissioning_date"])
        returned = CustodyReturnForm()
        assert f'value="{timezone.localdate().isoformat()}"' in str(returned["returned_on"])
