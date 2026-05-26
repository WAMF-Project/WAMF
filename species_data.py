SPECIES_DESCRIPTIONS = {

    "Turdus migratorius":
        "A migratory thrush commonly found in gardens, woodland edges, and urban green spaces.",

    "Cyanistes caeruleus":
        "A small colourful tit species known for agility, intelligence, and frequent feeder visits.",

    "Parus major":
        "A bold and adaptable woodland bird often seen dominating garden feeders.",

    "Passer domesticus":
        "A highly social urban sparrow closely associated with human settlements.",

    "Sturnus vulgaris":
        "A highly adaptable flocking bird known for iridescent plumage and complex vocal mimicry.",

    "Garrulus glandarius":
        "A striking woodland corvid recognised for bright plumage and loud warning calls.",

    "Dendrocopos major":
        "A widespread woodpecker species commonly identified by rhythmic tree drumming.",

    "Cyanocitta cristata":
        "An intelligent North American jay known for vivid blue plumage and loud vocalisations."
}


def get_species_description(scientific_name):

    return SPECIES_DESCRIPTIONS.get(
        scientific_name,
        "No species description available."
    )