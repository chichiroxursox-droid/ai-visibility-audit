"""best_category(): prefer a name-implied SPECIALTY over a broad Google category, without hijacking
correct specific categories. Regression guard for the 'brake specialist audited as general auto
repair' bug found in the 2026-06-19 multi-sample live run."""
from tools.common import best_category


def test_name_specialty_overrides_broad_auto_category():
    # the reported bug: a brake specialist filed by Google under the auto-repair umbrella
    assert best_category("Car repair and maintenance service", "Joe's Mobile Brake Repair") == ("brake repair", True)


def test_specific_matching_category_is_kept():
    # name agrees with category -> no override, not "inferred"
    assert best_category("Auto repair shop", "Joe's Auto Repair") == ("Auto repair shop", False)
    assert best_category("Plumber", "Bob's Plumbing") == ("Plumber", False)


def test_weak_keyword_does_not_hijack_a_correct_category():
    # "clean" is NOT a specialty keyword -> must not turn a chimney sweep into a cleaning service
    assert best_category("Chimney sweep", "Clean Sweep Chimney") == ("Chimney sweep", False)
    # "spa"/"tree" excluded too: a spa named with a metaphor keeps its category
    assert best_category("Day spa", "Tree of Life Spa") == ("Day spa", False)


def test_generic_category_still_infers_from_name():
    assert best_category("Services", "Joe's Mobile Brake Repair") == ("brake repair", True)
    assert best_category("Establishment", "New Choice Handy Services") == ("handyman", True)


def test_specialty_equal_to_category_is_a_noop():
    # name specialty resolves to the same trade the category already names -> keep category, not inferred
    assert best_category("Pressure washing service", "ProTeam Power Washing") == ("Pressure washing service", False)


def test_no_signal_falls_back_to_primary():
    assert best_category("Hair salon", "Lush Studio") == ("Hair salon", False)
    assert best_category(None, "Mystery LLC") == (None, False)


def test_instructional_name_pattern_yields_trade():
    # coaching/lessons businesses have no one-word trade keyword; the trade lives in the name
    assert best_category(None, "Main Line Basketball Lessons") == ("basketball lessons", True)
    assert best_category("Establishment", "Joe's Guitar Lessons") == ("guitar lessons", True)
    assert best_category(None, "Smith Math Tutoring") == ("math tutoring", True)
    assert best_category(None, "Elite Tennis Coaching") == ("tennis coaching", True)


def test_instructional_standalone_when_no_activity():
    # filler word before the suffix -> use the service word alone (only when it stands on its own)
    assert best_category(None, "Premier Tutoring") == ("tutoring", True)


def test_fitness_keywords_resolve():
    assert best_category(None, "Jane's Yoga Studio") == ("yoga instructor", True)
    assert best_category("Services", "Iron Will CrossFit") == ("fitness coaching", True)
